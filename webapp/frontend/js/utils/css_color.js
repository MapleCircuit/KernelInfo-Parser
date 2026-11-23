/**
 * css_color.js - DOM-free CSS color evaluation for code that draws outside CSS (exported NodeMap SVG / PNG,
 * canvas charts, native color inputs; `webapp/API_FRONTEND.md` §5.3).
 *
 * `parseCssColor()` reads `#rgb`, `#rgba`, `#rrggbb`, `#rrggbbaa`, `rgb()` / `rgba()` and `hsl()` / `hsla()`
 * (comma or space syntax, `/ alpha`, percentages), `transparent` and the basic named colors. `evalCssColor()` additionally
 * substitutes `var(--x[, fallback])` (nested, through a token reader) and evaluates
 * `color-mix(in srgb, A [p%], B [q%])` with the CSS Color 5 rules: missing percentages complete to 100%,
 * a sum other than 100% is normalized and a sum below 100% scales the alpha, channels are interpolated
 * premultiplied by alpha. Colors are `{r, g, b, a}` with 0–255 channels and 0–1 alpha.
 */

const NAMED = {
  black: [0, 0, 0], white: [255, 255, 255], red: [255, 0, 0], green: [0, 128, 0], blue: [0, 0, 255],
  yellow: [255, 255, 0], orange: [255, 165, 0], purple: [128, 0, 128], cyan: [0, 255, 255], magenta: [255, 0, 255],
  gray: [128, 128, 128], grey: [128, 128, 128], silver: [192, 192, 192], pink: [255, 192, 203],
};
const MAX_DEPTH = 16;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function channel(tok) {
  const t = String(tok).trim();
  if (t === "none") return 0;
  if (t.endsWith("%")) return clamp((parseFloat(t) / 100) * 255, 0, 255);
  const v = parseFloat(t);
  return Number.isFinite(v) ? clamp(v, 0, 255) : NaN;
}

function alphaOf(tok) {
  const t = String(tok).trim();
  if (t === "none") return 0;
  const v = t.endsWith("%") ? parseFloat(t) / 100 : parseFloat(t);
  return Number.isFinite(v) ? clamp(v, 0, 1) : NaN;
}

/** `{r, g, b, a}` of a plain CSS color (no `var()` / `color-mix()`), or null. */
export function parseCssColor(value) {
  const s = String(value ?? "").trim().toLowerCase();
  if (!s) return null;
  if (s === "transparent") return { r: 0, g: 0, b: 0, a: 0 };
  if (NAMED[s]) return { r: NAMED[s][0], g: NAMED[s][1], b: NAMED[s][2], a: 1 };
  let m = /^#([0-9a-f]{3,4})$/.exec(s);
  if (m) {
    const h = [...m[1]].map((x) => parseInt(x + x, 16));
    return { r: h[0], g: h[1], b: h[2], a: h.length > 3 ? h[3] / 255 : 1 };
  }
  m = /^#([0-9a-f]{6})([0-9a-f]{2})?$/.exec(s);
  if (m) {
    const h = (i) => parseInt(m[1].slice(i * 2, i * 2 + 2), 16);
    return { r: h(0), g: h(1), b: h(2), a: m[2] ? parseInt(m[2], 16) / 255 : 1 };
  }
  m = /^hsla?\(([^()]*)\)$/.exec(s);
  if (m) {
    const [body, alpha = "1"] = m[1].includes("/") ? m[1].split("/") : m[1].includes(",") && m[1].split(",").length === 4 ? [m[1].split(",").slice(0, 3).join(","), m[1].split(",")[3]] : [m[1]];
    const parts = body.trim().split(/[\s,]+/).filter(Boolean);
    if (parts.length !== 3) return null;
    const hue = ((parseFloat(parts[0]) % 360) + 360) % 360;
    const sat = clamp(parseFloat(parts[1]) / 100, 0, 1);
    const lig = clamp(parseFloat(parts[2]) / 100, 0, 1);
    const a = alphaOf(alpha);
    if ([hue, sat, lig, a].some((v) => Number.isNaN(v))) return null;
    const f = (k) => {
      const kk = (k + hue / 30) % 12;
      return (lig - sat * Math.min(lig, 1 - lig) * Math.max(-1, Math.min(kk - 3, 9 - kk, 1))) * 255;
    };
    return { r: f(0), g: f(8), b: f(4), a };
  }
  m = /^rgba?\(([^()]*)\)$/.exec(s);
  if (m) {
    let body = m[1].trim();
    let alpha = "1";
    let parts;
    if (body.includes(",")) {
      parts = body.split(",").map((x) => x.trim());
      if (parts.length === 4) alpha = parts.pop();
    } else {
      const slash = body.split("/");
      if (slash.length === 2) {
        body = slash[0];
        alpha = slash[1];
      }
      parts = body.trim().split(/\s+/);
    }
    if (parts.length !== 3) return null;
    const [r, g, b] = parts.map(channel);
    const a = alphaOf(alpha);
    if ([r, g, b, a].some((v) => Number.isNaN(v))) return null;
    return { r, g, b, a };
  }
  return null;
}

/** Split `s` on top-level commas (not inside parentheses). */
function splitTop(s) {
  const out = [];
  let depth = 0;
  let cur = "";
  for (const ch of s) {
    if (ch === "(") depth++;
    else if (ch === ")") depth--;
    if (ch === "," && depth === 0) {
      out.push(cur);
      cur = "";
    } else cur += ch;
  }
  out.push(cur);
  return out.map((x) => x.trim());
}

/** Index of the `)` closing the `(` at `open`, or -1. */
function closeOf(s, open) {
  let depth = 0;
  for (let i = open; i < s.length; i++) {
    if (s[i] === "(") depth++;
    else if (s[i] === ")" && --depth === 0) return i;
  }
  return -1;
}

/** `value` with every `var(--x[, fallback])` replaced by the token's value (recursively), or null when one is undefined. */
export function substituteVars(value, read, depth = 0) {
  let s = String(value ?? "");
  if (depth > MAX_DEPTH) return null;
  for (let guard = 0; guard < 256; guard++) {
    const at = s.indexOf("var(");
    if (at < 0) return s;
    const end = closeOf(s, at + 3);
    if (end < 0) return null;
    const [name, ...rest] = splitTop(s.slice(at + 4, end));
    let v = "";
    try {
      v = String((read && read(name.trim())) || "").trim();
    } catch {
      v = "";
    }
    if (!v && rest.length) v = rest.join(",").trim();
    if (!v) return null;
    const sub = substituteVars(v, read, depth + 1);
    if (sub === null) return null;
    s = `${s.slice(0, at)}${sub}${s.slice(end + 1)}`;
  }
  return null;
}

/** Split a color-mix() argument into its color and optional percentage. */
function mixArg(arg) {
  const m = /^(.*?)\s+(-?[\d.]+)%$/.exec(arg) || /^(-?[\d.]+)%\s+(.*)$/.exec(arg);
  if (!m) return { color: arg, p: null };
  return /%$/.test(arg) ? { color: m[1], p: Number(m[2]) } : { color: m[2], p: Number(m[1]) };
}

/** CSS Color 5 `color-mix(in srgb, …)` of two parsed colors; `p1` / `p2` are percentages or null. */
export function mixRgba(c1, c2, p1 = 50, p2 = null) {
  let a = p1;
  let b = p2;
  if (a == null && b == null) a = b = 50;
  else if (a == null) a = 100 - b;
  else if (b == null) b = 100 - a;
  const sum = a + b;
  if (!(sum > 0)) return null;
  const w1 = a / sum;
  const w2 = b / sum;
  const alpha = c1.a * w1 + c2.a * w2;
  const mult = sum < 100 ? sum / 100 : 1;
  if (alpha <= 0) return { r: 0, g: 0, b: 0, a: 0 };
  const ch = (k) => (c1[k] * c1.a * w1 + c2[k] * c2.a * w2) / alpha;
  return { r: ch("r"), g: ch("g"), b: ch("b"), a: alpha * mult };
}

function evalColor(s, depth) {
  if (depth > MAX_DEPTH) return null;
  const t = String(s).trim();
  const plain = parseCssColor(t);
  if (plain) return plain;
  const m = /^color-mix\(/i.exec(t);
  if (!m || closeOf(t, m[0].length - 1) !== t.length - 1) return null;
  const args = splitTop(t.slice(m[0].length, -1));
  if (args.length !== 3 || !/^in\s+srgb$/i.test(args[0])) return null;
  const x = mixArg(args[1]);
  const y = mixArg(args[2]);
  const c1 = evalColor(x.color, depth + 1);
  const c2 = evalColor(y.color, depth + 1);
  if (!c1 || !c2) return null;
  return mixRgba(c1, c2, x.p, y.p);
}

/** `{r, g, b, a}` of any supported CSS color expression (`var()` resolved through `read(name)`), or null. */
export function evalCssColor(value, read = null) {
  const s = substituteVars(value, read);
  return s === null ? null : evalColor(s, 0);
}

const hex2 = (v) => Math.round(clamp(v, 0, 255)).toString(16).padStart(2, "0");

/** `#rrggbb` (opaque) or `rgba(r, g, b, a)` of a parsed color. */
export function formatCssColor(c) {
  if (!c) return "";
  const a = Math.round(clamp(c.a, 0, 1) * 1000) / 1000;
  if (a >= 1) return `#${hex2(c.r)}${hex2(c.g)}${hex2(c.b)}`;
  return `rgba(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)}, ${a})`;
}

/** `#rrggbb` of the color's channels (alpha dropped), or null. */
export function hexOfCssColor(value, read = null) {
  const c = evalCssColor(value, read);
  return c ? `#${hex2(c.r)}${hex2(c.g)}${hex2(c.b)}` : null;
}

/**
 * `color-mix(in srgb, a pct%, b)` of two CSS colors as a resolved color string; null when either cannot
 * be read.
 */
export function mixCssColors(a, b, pct, read = null) {
  const c1 = evalCssColor(a, read);
  const c2 = evalCssColor(b, read);
  if (!c1 || !c2) return null;
  return formatCssColor(mixRgba(c1, c2, pct, null));
}

/**
 * SVG 1.1 paint of a color: `{ color: "#rrggbb", opacity }` (`opacity` 1 when opaque); a color that cannot
 * be parsed is returned as it is with opacity 1.
 */
export function svgPaint(value) {
  const c = parseCssColor(value);
  if (!c) return { color: String(value ?? ""), opacity: 1 };
  return { color: `#${hex2(c.r)}${hex2(c.g)}${hex2(c.b)}`, opacity: Math.round(clamp(c.a, 0, 1) * 1000) / 1000 };
}
