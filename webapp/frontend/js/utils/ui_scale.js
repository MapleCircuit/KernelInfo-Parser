/**
 * ui_scale.js - Shared UI Scale calculation & CSS length scaling helpers (webapp/API_FRONTEND.md §5.5).
 *
 * UI Scale adjusts typography via `--ui-font-scale` (0.8..1.6) and layout geometry via a 50% damped
 * `--ui-layout-scale = 1 + (fontScale - 1) * 0.5` (0.9..1.3). Code/diff editor lines and NodeMap
 * generated SVGs are intentionally exempt.
 */

export const DEFAULT_UI_FONT_SCALE = 100;
export const MIN_UI_FONT_SCALE = 80;
export const MAX_UI_FONT_SCALE = 160;

const scaleListeners = new Set();

/** Subscribe to UI scale changes (`{ uiFontScale, fontScale, layoutScale }`). Returns an unsubscribe function. */
export function onUiScaleChanged(callback) {
  scaleListeners.add(callback);
  return () => scaleListeners.delete(callback);
}

/** Notify UI scale subscribers (invoked by `state.emit("ui:scale-changed", ...)`). */
export function emitUiScaleChanged(detail) {
  for (const cb of scaleListeners) {
    try {
      cb(detail);
    } catch (err) {
      console.error("Error in ui:scale-changed listener:", err);
    }
  }
}

function readSavedUiScale() {
  try {
    if (typeof localStorage === "undefined") return null;
    const raw = localStorage.getItem("kip-ui-font-scale");
    return raw == null ? null : parseInt(raw, 10);
  } catch {
    return null;
  }
}

function readRootCssVar(name) {
  try {
    const el = typeof document !== "undefined" ? document.documentElement : null;
    if (!el) return NaN;
    const inlineVal = el.style?.getPropertyValue?.(name);
    if (inlineVal) {
      const n = parseFloat(inlineVal);
      if (Number.isFinite(n) && n > 0) return n;
    }
    if (typeof getComputedStyle === "function") {
      const computedVal = getComputedStyle(el)?.getPropertyValue?.(name);
      if (computedVal) {
        const n = parseFloat(computedVal);
        if (Number.isFinite(n) && n > 0) return n;
      }
    }
  } catch {
    // Node/headless environments without full DOM CSSOM
  }
  return NaN;
}

/**
 * Compute clamped `{ uiFontScale, fontScale, layoutScale }` from an explicit percentage (80..160)
 * or saved `localStorage` preference (defaulting to 100%).
 */
export function computeUiScale(uiFontScale = null) {
  let ufs = uiFontScale;
  if (ufs === null || ufs === undefined) {
    const saved = readSavedUiScale();
    ufs = saved !== null ? saved : DEFAULT_UI_FONT_SCALE;
  } else {
    ufs = Number(ufs);
  }
  ufs = Math.max(MIN_UI_FONT_SCALE, Math.min(MAX_UI_FONT_SCALE, Number.isFinite(ufs) ? Math.round(ufs) : DEFAULT_UI_FONT_SCALE));
  const fontScale = ufs / 100;
  const layoutScale = 1 + (fontScale - 1) * 0.5;
  return { uiFontScale: ufs, fontScale, layoutScale };
}

/** Current `--ui-layout-scale` multiplier (0.9..1.3, default 1). */
export function getUiLayoutScale() {
  const fromDom = readRootCssVar("--ui-layout-scale");
  if (Number.isFinite(fromDom) && fromDom > 0) return fromDom;
  return computeUiScale().layoutScale;
}

/** Current `--ui-font-scale` multiplier (0.8..1.6, default 1). */
export function getUiFontScale() {
  const fromDom = readRootCssVar("--ui-font-scale");
  if (Number.isFinite(fromDom) && fromDom > 0) return fromDom;
  return computeUiScale().fontScale;
}

/** Scale a base pixel measurement by `scale` (defaults to `getUiLayoutScale()`), rounded to integer px. */
export function scaleUiPx(basePx, scale = getUiLayoutScale()) {
  const n = Number(basePx);
  if (!Number.isFinite(n)) return basePx;
  const s = Number(scale);
  return Math.round(n * (Number.isFinite(s) && s > 0 ? s : 1));
}

/** Wrap literal `Npx` CSS length strings in `calc(Npx * var(--ui-layout-scale, 1))`; pass non-px strings through. */
export function scaleCssLength(value) {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (/^\d+(?:\.\d+)?px$/i.test(trimmed)) {
    return `calc(${trimmed} * var(--ui-layout-scale, 1))`;
  }
  return value;
}
