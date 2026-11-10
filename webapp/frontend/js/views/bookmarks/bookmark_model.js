/**
 * bookmark_model.js - Bookmarks, notes and code tours (G05, webapp/API.md §5.35): targets, line relocation,
 * validation of imported data and the compressed tour links. DOM-free, tested.
 *
 * Targets: {type: "line", path, rev, sha, line, snippet} | {type: "symbol", name, arch, rev} | {type: "commit", hash}
 *        | {type: "mail", tid, mid} | {type: "kconfig", name, arch, rev}
 */
export const MAX_TOUR_BYTES = 1 << 20;
const IDENT = /^[A-Za-z_]\w{0,127}$/;
const HEX = /^[0-9a-f]{7,40}$/i;
const REV = /^[\w./+~^-]{1,128}$/;

export function safeRepoPath(path) {
  const p = String(path || "");
  return p.length > 0 && p.length <= 1024 && !p.startsWith("/") && !p.includes("\\") && !p.includes("\0")
    && !p.split("/").some((s) => s === ".." || s === "") && !p.startsWith(".git/");
}

/** Whether a target is well formed (imported tours and notes are untrusted). */
export function validTarget(t) {
  if (!t || typeof t !== "object") return false;
  const rev = (r) => r === undefined || r === "" || (typeof r === "string" && REV.test(r) && !r.includes(".."));
  switch (t.type) {
    case "line": return safeRepoPath(t.path) && Number.isInteger(t.line) && t.line > 0 && rev(t.rev) && (!t.sha || HEX.test(t.sha))
      && (t.snippet === undefined || (typeof t.snippet === "string" && t.snippet.length <= 500));
    case "symbol": return IDENT.test(t.name || "") && rev(t.rev);
    case "kconfig": return /^(CONFIG_)?[A-Za-z0-9_]{1,128}$/.test(t.name || "") && rev(t.rev);
    case "commit": return HEX.test(t.hash || "");
    case "mail": return (Number.isInteger(t.tid) && t.tid >= 0) && typeof (t.mid ?? "") === "string" && String(t.mid ?? "").length <= 300;
    default: return false;
  }
}

export function targetLabel(t) {
  switch (t?.type) {
    case "line": return `${t.path}:${t.line}`;
    case "symbol": return `${t.name}()`;
    case "kconfig": return `CONFIG_${String(t.name).replace(/^CONFIG_/, "")}`;
    case "commit": return `commit ${String(t.hash).slice(0, 12)}`;
    case "mail": return t.mid ? `mail <${t.mid}>` : `thread ${t.tid}`;
    default: return "?";
  }
}

/** Same place (for toggling a bookmark): a line by path and line, others by identity. */
export function sameTarget(a, b) {
  if (!a || !b || a.type !== b.type) return false;
  if (a.type === "line") return a.path === b.path && a.line === b.line && (a.sha || a.rev) === (b.sha || b.rev);
  return targetLabel(a) === targetLabel(b);
}

/**
 * Find a bookmarked line again in another text of the file: the line itself when it still holds the snippet,
 * else the nearest exact match within ±radius lines (`moved`), else null.
 */
export function relocate(lines, line, snippet, radius = 200) {
  const want = String(snippet || "").trim();
  if (!want) return lines.length >= line ? { line, moved: false } : null;
  const at = (n) => (lines[n - 1] ?? "").trim() === want;
  if (at(line)) return { line, moved: false };
  for (let d = 1; d <= radius; d++) {
    if (at(line - d)) return { line: line - d, moved: true };
    if (at(line + d)) return { line: line + d, moved: true };
  }
  return null;
}

/** Bookmarks in display order: group, then `order`, then creation. */
export function sortBookmarks(records) {
  return [...records].sort((a, b) => String(a.data.group || "").localeCompare(String(b.data.group || ""))
    || (a.data.order ?? 0) - (b.data.order ?? 0) || (a.created_at || 0) - (b.created_at || 0));
}

/** A tour as stored and shared, checked: {title, description, steps: [{target, title, markdown}]}; throws when invalid. */
export function validateTour(tour) {
  const json = JSON.stringify(tour ?? null);
  if (json.length > MAX_TOUR_BYTES) throw new Error("A tour is limited to 1 MB");
  if (!tour || typeof tour.title !== "string" || !tour.title.trim() || tour.title.length > 200) throw new Error("A tour needs a title (at most 200 characters)");
  if (!Array.isArray(tour.steps) || tour.steps.length > 500) throw new Error("A tour has at most 500 steps");
  const steps = tour.steps.map((s, i) => {
    if (!validTarget(s?.target)) throw new Error(`Step ${i + 1} has an invalid target`);
    return { target: s.target, title: String(s.title || "").slice(0, 200), markdown: String(s.markdown || "").slice(0, 50000) };
  });
  return { title: tour.title.trim(), description: String(tour.description || "").slice(0, 20000), steps };
}

const unb64url = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));

async function pipe(bytes, stream) {
  const out = new Response(new Blob([bytes]).stream().pipeThrough(stream));
  return new Uint8Array(await out.arrayBuffer());
}

/** `t=` of a tour link: base64url(deflate-raw(json)). */
export async function encodeTour(tour) {
  const bytes = new TextEncoder().encode(JSON.stringify(validateTour(tour)));
  return chunkedB64(await pipe(bytes, new CompressionStream("deflate-raw")));
}

function chunkedB64(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function decodeTour(param) {
  if (typeof param !== "string" || param.length > 2 * MAX_TOUR_BYTES) throw new Error("Invalid tour link");
  const bytes = await pipe(unb64url(param), new DecompressionStream("deflate-raw"));
  if (bytes.length > MAX_TOUR_BYTES) throw new Error("A tour is limited to 1 MB");
  return validateTour(JSON.parse(new TextDecoder().decode(bytes)));
}
