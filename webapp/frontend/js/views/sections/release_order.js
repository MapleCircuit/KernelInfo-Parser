/**
 * release_order.js - Release tag ordering on the client, the same rules as the server's release index
 * (parser/git_ast/release_order.py, webapp/backend/services/release_service.py, webapp/API.md §2.17). Pure.
 *
 * Two tag schemes, each its own family of chains:
 * - the kernel scheme (family ""): `v3.1-rc2`, `v3.0.5`, `v2.6.39.4`;
 * - prefixed releases (family = the prefix): `<prefix>X.Y[.Z][-rcN|-preN]`, e.g. `man-pages-6.9.1`,
 *   `foo-1.2.3`; X.Y is the series and Z its point release, compared as numbers (`man-pages-6.04` and
 *   `man-pages-6.4` are one series). A prefixed series sorts after every kernel series, series of different
 *   prefixes by prefix.
 * `releaseTagNames()` picks a repository's release tags: the kernel scheme's when it has any, else every
 * prefixed one.
 */
const TAG_RE = /^v(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-(rc|pre)(\d+))?$/;
// <prefix>X.Y[.Z][-rcN|-preN]: the prefix starts with a letter and holds no dot
const PREFIXED_TAG_RE = /^([A-Za-z][A-Za-z0-9_+-]*?)(\d+)\.(\d+)(?:\.(\d+))?(?:-(rc|pre)(\d+))?$/;
export const KERNEL_FAMILY = "";
// First element of a prefixed series: [PREFIXED_SERIES, prefix, X, Y]
export const PREFIXED_SERIES = 1000;

function parseKernel(m) {
  const [, a, b, c, d, , preNum] = m;
  let series;
  let stable;
  if (+a === 2 && +b === 6) {
    if (c === undefined) return null;
    series = [2, 6, +c];
    stable = d !== undefined ? +d : 0;
  } else {
    if (d !== undefined) return null;
    series = [+a, +b, 0];
    stable = c !== undefined ? +c : 0;
  }
  const pre = preNum !== undefined ? +preNum : 0;
  if (pre && stable) return null;
  return { series, stable, pre, final: !pre && !stable, family: KERNEL_FAMILY };
}

/**
 * `v3.1-rc2` -> {series: [3, 1, 0], stable: 0, pre: 2, final: false, family: ""}; `man-pages-6.9.1` ->
 * {series: [1000, "man-pages-", 6, 9], stable: 1, ..., family: "man-pages-", base: "man-pages-6.9"} (`base`:
 * the tag's own spelling of its series); null for other names.
 */
export function parseTag(tag) {
  const name = String(tag || "");
  const m = TAG_RE.exec(name);
  if (m) return parseKernel(m);
  const p = PREFIXED_TAG_RE.exec(name);
  if (!p || p[1] === "v" || p[1] === "V") return null;
  const [, prefix, major, minor, point, , preNum] = p;
  const stable = point !== undefined ? +point : 0;
  const pre = preNum !== undefined ? +preNum : 0;
  if (pre && stable) return null;
  return { series: [PREFIXED_SERIES, prefix, +major, +minor], stable, pre, final: !pre && !stable, family: prefix, base: `${prefix}${major}.${minor}` };
}

/** Family of a release tag ("" for the kernel scheme, else its prefix); null for other names. */
export function familyOf(tag) {
  const p = parseTag(tag);
  return p ? p.family : null;
}

/** The release tags among `names` (order kept): the kernel scheme's when any name uses it, else every prefixed one. */
export function releaseTagNames(names) {
  const parsed = [];
  for (const n of names || []) {
    const p = parseTag(n);
    if (p) parsed.push([n, p]);
  }
  if (parsed.some(([, p]) => p.family === KERNEL_FAMILY)) return parsed.filter(([, p]) => p.family === KERNEL_FAMILY).map(([n]) => n);
  return parsed.map(([n]) => n);
}

/** Comparable key (zero-padded string): rc < release < stable updates; kernel series before prefixed ones. */
export function releaseKey(tag) {
  const p = parseTag(tag);
  if (!p) return `0|${tag}`;
  const pad = (n) => (typeof n === "number" ? String(n).padStart(5, "0") : n);
  return `1|${p.series.map(pad).join(".")}|${p.pre ? 0 : 1}|${pad(p.pre)}|${pad(p.stable)}`;
}

export function isFinalRelease(tag) {
  return Boolean(parseTag(tag)?.final);
}

export function compareReleases(a, b) {
  const ka = releaseKey(a);
  const kb = releaseKey(b);
  return ka < kb ? -1 : ka > kb ? 1 : 0;
}

/** Series name of a release ("v3.0.5" -> "v3.0", "v2.6.39.4" -> "v2.6.39", "man-pages-6.04.1" -> "man-pages-6.4"). */
export function seriesOf(tag) {
  const p = parseTag(tag);
  if (!p) return "";
  if (p.series[0] === PREFIXED_SERIES) return `${p.series[1]}${p.series[2]}.${p.series[3]}`;
  return p.series[0] === 2 && p.series[1] === 6 ? `v2.6.${p.series[2]}` : `v${p.series[0]}.${p.series[1]}`;
}

/** Same series (`series` arrays of parseTag). */
export const sameSeries = (a, b) => Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((x, i) => x === b[i]);

/**
 * Release (Tag dropdown entry) of a prefixed release tag in a repository without kernel tags: its series'
 * final tag (`finals`: Map series name -> final tag name, whatever its spelling) else the tag's own spelling of
 * the series; `{base, suffix, isSub}` like app.js parseTagVersion, null for a tag of no prefixed series.
 */
export function prefixedReleaseOf(tag, finals = null) {
  const p = parseTag(tag);
  if (!p || p.family === KERNEL_FAMILY) return null;
  const base = finals?.get(seriesOf(tag)) || p.base;
  if (p.final) return { base: tag, suffix: "", isSub: false };
  return { base, suffix: String(tag).startsWith(p.base) ? String(tag).slice(p.base.length) : `-${p.pre ? `rc${p.pre}` : p.stable}`, isSub: true };
}

/** Map series name -> final tag of the prefixed releases among `names` (first spelling wins). */
export function prefixedFinals(names) {
  const out = new Map();
  for (const n of names || []) {
    const p = parseTag(n);
    if (p && p.family !== KERNEL_FAMILY && p.final && !out.has(seriesOf(n))) out.set(seriesOf(n), n);
  }
  return out;
}
