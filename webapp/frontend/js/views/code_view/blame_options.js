/**
 * blame_options.js - Blame options and "blame before this change" (H9, webapp/API.md §5.21).
 *
 * Pure and DOM-free (webapp/frontend/tests/blame_options.test.mjs).
 * Options: follow moves (-M), follow copies (-C, slower), ignore whitespace (-w), written on the tab
 * as `blameOpts` and in the URL as `bo=<m|c|w letters>`.
 */

export const BLAME_OPTION_LETTERS = { move: "m", copy: "c", ws: "w" };

/** "mcw" -> {move, copy, ws}; unknown letters are ignored. */
export function parseBlameOpts(text) {
  const s = String(text || "");
  return { move: s.includes("m"), copy: s.includes("c"), ws: s.includes("w") };
}

/** {move, copy, ws} -> "mcw" (letters in that order; "" for none). */
export function formatBlameOpts(opts) {
  if (!opts) return "";
  return Object.entries(BLAME_OPTION_LETTERS).filter(([k]) => opts[k]).map(([, l]) => l).join("");
}

/** Query string of the blame endpoint for the options ("" when none). */
export function blameQuery(opts) {
  const o = typeof opts === "string" ? parseBlameOpts(opts) : opts || {};
  const params = new URLSearchParams();
  if (o.move) params.set("move", "1");
  if (o.copy) params.set("copy", "1");
  if (o.ws) params.set("ws", "1");
  return params.toString();
}

/**
 * Where "blame before this change" goes for a blamed line: the parent of the commit that last touched
 * it (`previous_sha`), the file as it was named there (`previous_path`), at about the same line
 * (`orig_line` is the line in the blamed commit's version). Null for a root commit.
 */
export function blameBeforeTarget(blame, currentPath) {
  if (!blame || !blame.previous_sha) return null;
  return {
    version: blame.previous_sha,
    path: blame.previous_path || blame.source_path || currentPath,
    line: Math.max(1, Number(blame.orig_line) || Number(blame.line) || 1),
  };
}

/** "moved from <path>" marker of a line whose blamed source is another file (-M/-C followed it). */
export function movedFrom(blame, currentPath) {
  const src = blame?.source_path;
  return src && currentPath && src !== currentPath ? src : "";
}
