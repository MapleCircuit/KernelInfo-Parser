/**
 * find_model.js - Matching for the code view's find bar (webapp/API.md §5.21, Find in file).
 *
 * Pure and DOM-free (webapp/frontend/tests/find_model.test.mjs). The code view is virtualized, so the
 * search runs over the raw text of the whole file (or the rows of a diff), never over the DOM.
 *
 * A match is {row, side, start, end}: `row` is the 0-based row of the viewport (the line index for a
 * plain file, the row or pair index of a diff), `side` is null, or "l"/"r" for the halves of a
 * side-by-side diff; `start`/`end` are UTF-16 offsets in that row's text.
 */

export const FIND_MATCH_CAP = 10000;
export const FIND_CHUNK_ROWS = 20000;

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Compile a query: {re, error}. Literal queries are escaped; whole word wraps the pattern in
 * identifier boundaries (letters, digits and "_", so `rq` does not match inside `rq_lock`).
 */
export function buildMatcher(query, { caseSensitive = false, wholeWord = false, regex = false } = {}) {
  if (!query) return { re: null, error: null };
  let source = regex ? query : escapeRegExp(query);
  if (wholeWord) source = `(?<![A-Za-z0-9_])(?:${source})(?![A-Za-z0-9_])`;
  try {
    return { re: new RegExp(source, caseSensitive ? "gu" : "giu"), error: null };
  } catch (err) {
    try {
      // Patterns valid without the Unicode flag (lone "\-", "{" ...) still work
      return { re: new RegExp(source, caseSensitive ? "g" : "gi"), error: null };
    } catch {
      return { re: null, error: String(err.message || err).replace(/^Invalid regular expression: /, "") };
    }
  }
}

/** Ranges [[start, end], ...] of `re` in one line; zero-width matches are skipped, never looped on. */
export function matchLine(text, re, limit = Infinity) {
  const out = [];
  if (!re || text == null) return out;
  re.lastIndex = 0;
  let m;
  while (out.length < limit && (m = re.exec(text)) !== null) {
    const start = m.index;
    const end = start + m[0].length;
    if (end === start) {
      // Advance past the empty match (by a whole code point under the "u" flag)
      re.lastIndex = start + (re.unicode && text.codePointAt(start) > 0xffff ? 2 : 1);
      if (re.lastIndex > text.length) break;
      continue;
    }
    out.push([start, end]);
  }
  return out;
}

/**
 * Rows to search for a view: [{row, side, text}]. `fileDiff` follows VirtualEditor's model
 * ({layout: "unified"|"split", rows: [{t}], pairs: [{l, r}]}); without it the raw lines are used.
 */
export function searchRows(rawLines, fileDiff = null) {
  if (!fileDiff) return rawLines.map((text, row) => ({ row, side: null, text }));
  if (fileDiff.layout === "split") {
    const out = [];
    fileDiff.pairs.forEach((p, row) => {
      if (p?.l) out.push({ row, side: "l", text: p.l.t || "" });
      if (p?.r) out.push({ row, side: "r", text: p.r.t || "" });
    });
    return out;
  }
  return fileDiff.rows.map((r, row) => ({ row, side: null, text: r?.t || "" }));
}

/** Every match of `re` in `rows` ({row, side, text}), at most `cap`: {matches, capped}. */
export function findInRows(rows, re, cap = FIND_MATCH_CAP, from = 0, to = rows.length) {
  const matches = [];
  let capped = false;
  for (let i = from; i < to; i++) {
    const { row, side, text } = rows[i];
    const ranges = matchLine(text, re, cap - matches.length + 1);
    for (const [start, end] of ranges) {
      if (matches.length >= cap) {
        capped = true;
        break;
      }
      matches.push({ row, side, start, end });
    }
    if (capped) break;
  }
  return { matches, capped };
}

/** Synchronous search of plain lines: {matches, capped, error}. */
export function findAll(lines, query, opts = {}, cap = FIND_MATCH_CAP) {
  const { re, error } = buildMatcher(query, opts);
  if (!re) return { matches: [], capped: false, error };
  return { ...findInRows(searchRows(lines), re, cap), error: null };
}

/**
 * Search in chunks of FIND_CHUNK_ROWS rows, yielding between them (`idle`, requestIdleCallback in the
 * browser) and reporting the running count; resolves to {matches, capped, error, cancelled}.
 */
export async function findAllChunked(rows, re, { cap = FIND_MATCH_CAP, chunk = FIND_CHUNK_ROWS, onProgress = null,
  isCancelled = () => false, idle = defaultIdle } = {}) {
  const matches = [];
  for (let from = 0; from < rows.length; from += chunk) {
    if (isCancelled()) return { matches, capped: false, error: null, cancelled: true };
    const part = findInRows(rows, re, cap - matches.length, from, Math.min(rows.length, from + chunk));
    matches.push(...part.matches);
    if (part.capped || matches.length >= cap) return { matches, capped: true, error: null, cancelled: false };
    if (onProgress) onProgress(matches.length, Math.min(rows.length, from + chunk), rows.length);
    if (from + chunk < rows.length) await idle();
  }
  return { matches, capped: false, error: null, cancelled: false };
}

function defaultIdle() {
  return new Promise((resolve) => {
    if (typeof requestIdleCallback === "function") requestIdleCallback(() => resolve(), { timeout: 50 });
    else setTimeout(resolve, 0);
  });
}

function compare(a, b) {
  if (a.row !== b.row) return a.row - b.row;
  const sa = a.side === "l" ? 0 : 1;
  const sb = b.side === "l" ? 0 : 1;
  if (sa !== sb) return sa - sb;
  return a.start - b.start;
}

/**
 * Index of the next (dir > 0) or previous match from position {row, side, start}, wrapping around;
 * -1 when there are no matches.
 */
export function stepMatch(matches, current, dir, pos = null) {
  if (!matches.length) return -1;
  if (current >= 0 && current < matches.length && !pos) {
    return (current + (dir > 0 ? 1 : -1) + matches.length) % matches.length;
  }
  const at = pos || { row: 0, side: null, start: -1 };
  if (dir > 0) {
    const idx = matches.findIndex((m) => compare(m, at) > 0);
    return idx === -1 ? 0 : idx;
  }
  for (let i = matches.length - 1; i >= 0; i--) {
    if (compare(matches[i], at) < 0) return i;
  }
  return matches.length - 1;
}

/** First match at or after row `row` (0 when none is): where a fresh search starts. */
export function firstMatchFrom(matches, row) {
  if (!matches.length) return -1;
  const idx = matches.findIndex((m) => m.row >= row);
  return idx === -1 ? 0 : idx;
}

/** Matches grouped by "row|side" for rendering: Map(key -> [[start, end, globalIndex], ...]). */
export function groupByRow(matches) {
  const out = new Map();
  matches.forEach((m, i) => {
    const key = `${m.row}|${m.side || ""}`;
    if (!out.has(key)) out.set(key, []);
    out.get(key).push([m.start, m.end, i]);
  });
  return out;
}

/**
 * Where to split a row's text nodes for highlighting: given the lengths of the row's text nodes (in
 * document order) and the match ranges of the row, returns [{node, start, end, index}] pieces, each
 * within a single text node (a match spanning token boundaries becomes several pieces).
 */
export function rangesToNodePieces(nodeLengths, ranges) {
  const pieces = [];
  const starts = [];
  let offset = 0;
  for (const len of nodeLengths) {
    starts.push(offset);
    offset += len;
  }
  for (const [start, end, index] of ranges) {
    for (let n = 0; n < nodeLengths.length; n++) {
      const a = starts[n];
      const b = a + nodeLengths[n];
      if (b <= start || a >= end) continue;
      pieces.push({ node: n, start: Math.max(start, a) - a, end: Math.min(end, b) - a, index });
    }
  }
  // Split from the end of each node first, so earlier offsets stay valid while wrapping
  pieces.sort((x, y) => x.node - y.node || y.start - x.start);
  return pieces;
}

/** "3/120", "0 results", "1/10000+" */
export function countLabel(current, total, capped) {
  if (!total) return "No results";
  return `${current >= 0 ? current + 1 : "?"}/${total}${capped ? "+" : ""}`;
}
