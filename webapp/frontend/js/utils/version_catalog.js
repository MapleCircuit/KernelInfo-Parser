/**
 * version_catalog.js - Codec of the compact version catalog (GET /api/versions?compact=1, webapp/API.md §4.1).
 *
 * A catalog is kept as {g, r}: `g` the server's generation, `r` positional rows
 * [vname, sha12, parsed, ref_type] (`parsed`: parse completion time, null for a tag only Git has).
 * The server sends either the full catalog as columns or, for a client naming the generation it has,
 * the operations turning that generation into the current one.
 */

// Layout version: part of the ETag the client sends (If-None-Match) and of the IndexedDB key
export const VERSION_CATALOG_FORMAT = "c1";
const SHA_LEN = 12;
// A ref without a known commit in the hash column (Git's null object id)
const NULL_SHA12 = "0".repeat(SHA_LEN);
// Same test as the server's PRERELEASE_MARKERS (webapp/backend/routers/versions.py)
const PRERELEASE_RE = /-(?:rc|pre|alpha|beta)/;

/** Rows of a full catalog body {g, n: names, h: 12-hex chunks, p: parse times, t: [index, ref_type] pairs}. */
function rowsFromColumns({ n = [], h = "", p = [], t = [] }) {
  const rows = n.map((name, i) => {
    const sha = h.slice(i * SHA_LEN, (i + 1) * SHA_LEN);
    return [name, sha === NULL_SHA12 ? "" : sha, p[i] ?? null, 1];
  });
  for (const [i, type] of t) if (rows[i]) rows[i][3] = type;
  return rows;
}

/**
 * Rows of `base` changed by delta operations: [start, count] copies rows of `base`, any other entry is a
 * row [vname, sha12, parsed?, ref_type?]. Null when an operation does not fit `base`.
 */
function applyOps(base, ops) {
  const rows = [];
  for (const op of ops) {
    if (typeof op[0] === "number") {
      const [start, count] = op;
      if (!(start >= 0 && count >= 0 && start + count <= base.length)) return null;
      for (let i = start; i < start + count; i++) rows.push(base[i]);
    } else {
      const [name, sha = "", parsed = null, type = 1] = op;
      rows.push([name, sha, parsed, type]);
    }
  }
  return rows;
}

/**
 * The catalog a response body describes: a full catalog, or a delta applied to `base` (the catalog the
 * request named). Null when the body is neither, or a delta of another base.
 */
export function catalogFromBody(body, base) {
  if (!body || typeof body.g !== "string") return null;
  if (Array.isArray(body.d)) {
    if (!base || body.b !== base.g) return null;
    const rows = applyOps(base.r, body.d);
    return rows && { g: body.g, r: rows };
  }
  return Array.isArray(body.n) ? { g: body.g, r: rowsFromColumns(body) } : null;
}

/** A stored catalog ({g, r}) is usable. */
export function isCatalog(c) {
  return Boolean(c) && typeof c.g === "string" && Array.isArray(c.r);
}

/**
 * The entries every view reads: {vname, commit_hash (12 hex), is_prerelease, ref_type, indexed, stamp}, the
 * stamp being the server's (`<sha12>.<parse time>` or `<sha12>.git`, webapp/API.md §2.11).
 */
export function versionsFromCatalog(c) {
  return c.r.map(([vname, sha, parsed, type]) => ({
    vname,
    commit_hash: sha,
    is_prerelease: PRERELEASE_RE.test(vname),
    ref_type: type,
    indexed: parsed !== null,
    stamp: `${sha}.${parsed === null ? "git" : parsed}`,
  }));
}
