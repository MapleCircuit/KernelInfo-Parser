/**
 * theme.js - Themes (G08, webapp/API.md §5.5): the setting, applying it, and design tokens for code that
 * cannot use CSS variables (canvas drawing, exported SVG files).
 *
 * `data-theme` on <html> is `dark` | `light` | `hc-dark` | `hc-light` | `auto` (boot.js sets it before the app
 * loads, from localStorage "kip-theme"); css/variables.css redefines the tokens per theme.
 */
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

/** A color usable outside CSS: `var(--x)` (or `var(--x, fallback)`) resolved to the token's value. */
export function resolveColor(value, read = undefined) {
  const m = /^var\((--[\w-]+)(?:,\s*([^)]+))?\)$/.exec(String(value || "").trim());
  return m ? token(m[1], m[2] || "", read) : value;
}
