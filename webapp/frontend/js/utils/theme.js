/**
 * theme.js - Themes (G08, webapp/API.md §5.5): the setting, applying it, and design tokens for code that
 * cannot use CSS variables (canvas drawing, exported SVG files).
 *
 * `data-theme` on <html> is `dark` | `light` | `hc-dark` | `hc-light` | `auto` (boot.js sets it before the app
 * loads, from localStorage "kip-theme"); css/variables.css redefines the tokens per theme.
 */
import { evalCssColor, formatCssColor, substituteVars } from "./css_color.js";

export const THEMES = [["auto", "Auto (follow the system)"], ["dark", "Dark"], ["light", "Light"], ["hc-dark", "High-contrast dark"], ["hc-light", "High-contrast light"]];
const KEY = "kip-theme";

export function savedTheme(storage = globalThis.localStorage) {
  try {
    const t = storage?.getItem(KEY);
    return THEMES.some(([id]) => id === t) ? t : "dark";
  } catch {
    return "dark";
  }
}

/** Switch theme now (no reload) and remember it; listeners of `kip:theme-changed` redraw. */
export function applyTheme(theme) {
  const t = THEMES.some(([id]) => id === theme) ? theme : "dark";
  document.documentElement.dataset.theme = t;
  try {
    localStorage.setItem(KEY, t);
  } catch {
    // not remembered
  }
  window.dispatchEvent(new CustomEvent("kip:theme-changed", { detail: { theme: t } }));
}

export function onThemeChange(cb) {
  const fn = () => cb(document.documentElement.dataset.theme || "dark");
  window.addEventListener("kip:theme-changed", fn);
  const mq = window.matchMedia?.("(prefers-color-scheme: light)");
  const sys = () => {
    if (document.documentElement.dataset.theme === "auto") fn();
  };
  mq?.addEventListener?.("change", sys);
  return () => {
    window.removeEventListener("kip:theme-changed", fn);
    mq?.removeEventListener?.("change", sys);
  };
}

/** The current value of a design token (`--bg-canvas`), or `fallback` when it is not defined. */
export function token(name, fallback = "", read = (n) => getComputedStyle(document.documentElement).getPropertyValue(n)) {
  try {
    const v = String(read(name) || "").trim();
    return v || fallback;
  } catch {
    return fallback;
  }
}

/**
 * A color usable outside CSS: `var()` (nested, with fallbacks) substituted from the tokens and
 * `color-mix(in srgb, …)` / `rgb()` / `rgba()` / hex evaluated (`utils/css_color.js`), as `#rrggbb` when
 * opaque and `rgba(r, g, b, a)` otherwise; a value that is not a color comes back with its `var()`s
 * substituted (`""` when a token is undefined).
 */
export function resolveColor(value, read = undefined) {
  const get = (n) => token(n, "", read);
  const c = evalCssColor(value, get);
  if (c) return formatCssColor(c);
  const s = substituteVars(value, get);
  return s === null ? "" : s;
}

/** A font family token (`--font-sans`) for an SVG `font-family` attribute (double quotes become single). */
export function resolveFont(name, fallback, read = undefined) {
  const v = substituteVars(token(name, fallback, read), (n) => token(n, "", read));
  return String(v || fallback).replace(/"/g, "'").replace(/[<>&]/g, "").trim() || fallback;
}
