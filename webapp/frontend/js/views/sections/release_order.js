/**
 * release_order.js - Release tag ordering on the client, the same rules as the server's release index
 * (webapp/backend/services/release_service.py, webapp/API.md §2.17). Pure.
 */
const TAG_RE = /^v(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-(rc|pre)(\d+))?$/;

export function parseTag(tag) {
  const m = TAG_RE.exec(String(tag || ""));
  if (!m) return null;
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
  return { series, stable, pre, final: !pre && !stable };
}

/** Comparable key (zero-padded string): rc < release < stable updates. */
export function releaseKey(tag) {
  const p = parseTag(tag);
  if (!p) return `0|${tag}`;
  const pad = (n) => String(n).padStart(5, "0");
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

/** Series name of a release ("v3.0.5" -> "v3.0", "v2.6.39.4" -> "v2.6.39"). */
export function seriesOf(tag) {
  const p = parseTag(tag);
  if (!p) return "";
  return p.series[0] === 2 && p.series[1] === 6 ? `v2.6.${p.series[2]}` : `v${p.series[0]}.${p.series[1]}`;
}
