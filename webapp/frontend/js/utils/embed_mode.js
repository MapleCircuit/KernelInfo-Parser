/**
 * embed_mode.js - Isolated embed shell (`/?embed=1#<tab hash>`, `webapp/API_FRONTEND.md` §5.3.4).
 *
 * A NodeMap tab node shows a live KernelInfo tab in an iframe of the app opened with `?embed=1`. That
 * instance is a guest of the window around it: it joins no workspace session (no session picker, no
 * window heartbeat or session pinning in the shared sessionStorage), persists nothing (tab state, session
 * index), registers no service worker, runs no sync check and hides the application chrome. Its theme and
 * UI scale follow the host window through `storage` events. Dependency-free (imported by state.js and
 * session_manager.js before anything else runs).
 *
 * `&embedSession=<key>` joins the shell to a NodeMap-scoped embedded session: the shells of one key (and
 * the NodeMap hosting them) talk over their own `BroadcastChannel` (`embedSessionChannel(key)`), so
 * Kconfig engines and assignments flow between them, while the workspace sessions (session index,
 * Switch Session, the `kip_multi_tab` channel) never see them.
 */

/** True when this page is an embed shell. */
export function isEmbedMode(loc = typeof location !== "undefined" ? location : null) {
  try {
    return new URLSearchParams(loc?.search || "").get("embed") === "1";
  } catch {
    return false;
  }
}

/** Storage keys whose change in the host window the embed shell re-applies. */
export const EMBED_FOLLOWED_KEYS = ["kip-theme", "kip-ui-font-scale", "kip-code-font-size"];

/** Key format of a NodeMap embedded session (`<nodemap tab id>:<session id>`). */
export const EMBED_SESSION_RE = /^[\w.:-]{1,160}$/;

/** The NodeMap embedded session this embed shell joined (`?embed=1&embedSession=<key>`), or `""`. */
export function embedSessionId(loc = typeof location !== "undefined" ? location : null) {
  try {
    const params = new URLSearchParams(loc?.search || "");
    if (params.get("embed") !== "1") return "";
    const key = params.get("embedSession") || "";
    return EMBED_SESSION_RE.test(key) ? key : "";
  } catch {
    return "";
  }
}

/** `BroadcastChannel` name of a NodeMap embedded session (never the workspace's `kip_multi_tab`). */
export function embedSessionChannel(key) {
  return `kip_nodemap_session:${key}`;
}

// ------------------------------------------------------------------ host bridge (NodeMap tab nodes)

/**
 * `postMessage` types between an embed shell and the NodeMap holding it (same origin only; the host also
 * checks that the message comes from the iframe of the node the token names). `hello`: the shell started
 * (`{node}`), the host answers `restore` (`{node, viewState}`); `state`: the shell's current address and
 * view state (`{node, hash, title, viewState}`, debounced); `escape`: Esc hands the pointer back.
 */
export const EMBED_MSG = Object.freeze({
  escape: "kip-embed-escape",
  hello: "kip-embed-hello",
  state: "kip-embed-state",
  restore: "kip-embed-restore",
});

/** Format of the node token of `&embedNode=<node id>`. */
export const EMBED_NODE_RE = /^[\w-]{1,64}$/;

/** The NodeMap node this embed shell renders (`?embed=1&embedNode=<id>`), or `""`. */
export function embedNodeToken(loc = typeof location !== "undefined" ? location : null) {
  try {
    const params = new URLSearchParams(loc?.search || "");
    if (params.get("embed") !== "1") return "";
    const id = params.get("embedNode") || "";
    return EMBED_NODE_RE.test(id) ? id : "";
  } catch {
    return "";
  }
}

/** Largest serialized view state a tab node keeps (JSON characters). */
export const EMBED_VIEWSTATE_MAX = 32768;
const VS_SCROLL_MAX = 24;
const VS_FIELDS_MAX = 48;
const VS_FIELD_RE = /^[A-Za-z_$][\w$]{0,63}$/;
// Tab fields that are the tab's identity or address (the hash carries those) or one-shot requests
const VS_SKIP_FIELDS = new Set(["id", "type", "repo", "version", "title", "path", "scrollPositions", "forceNew", "pinned"]);
const VS_SKIP_PREFIX = /^(initial|pending|transfer|__)/;
// Names that would reach an object's prototype machinery instead of a plain field
const VS_RESERVED = new Set(["__proto__", "constructor", "prototype", "toString", "valueOf", "hasOwnProperty", "toJSON"]);

function vsPrimitive(v, maxLen) {
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return Number.isFinite(v) ? v : undefined;
  if (typeof v === "string") return v.length <= maxLen ? v : undefined;
  return undefined;
}

/**
 * Bounded, JSON-safe view state of an embedded tab beyond its `#hash`: `scroll` (the tab's
 * `scrollPositions`, `components/scroll_memory.js` keys → `[top, left]`) and `fields` (its view-local
 * primitive fields: search text, filters, selection; booleans, finite numbers, strings up to 2000
 * characters and arrays of up to 200 of them). Null when nothing is left. Anything over
 * `EMBED_VIEWSTATE_MAX` drops `fields` first, then everything.
 */
export function normalizeEmbedViewState(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out = {};
  if (raw.scroll && typeof raw.scroll === "object" && !Array.isArray(raw.scroll)) {
    const scroll = {};
    let n = 0;
    for (const [k, v] of Object.entries(raw.scroll)) {
      if (n >= VS_SCROLL_MAX) break;
      if (k.length > 512 || !Array.isArray(v) || v.length !== 2) continue;
      const top = Math.round(Number(v[0]));
      const left = Math.round(Number(v[1]));
      if (!(top >= 0 && left >= 0 && top <= 1e7 && left <= 1e7) || (!top && !left)) continue;
      scroll[k] = [top, left];
      n += 1;
    }
    if (n) out.scroll = scroll;
  }
  if (raw.fields && typeof raw.fields === "object" && !Array.isArray(raw.fields)) {
    const fields = {};
    let n = 0;
    for (const [k, v] of Object.entries(raw.fields)) {
      if (n >= VS_FIELDS_MAX) break;
      if (!VS_FIELD_RE.test(k) || VS_SKIP_FIELDS.has(k) || VS_SKIP_PREFIX.test(k) || VS_RESERVED.has(k)) continue;
      let val;
      if (Array.isArray(v)) {
        if (v.length > 200) continue;
        val = v.map((x) => vsPrimitive(x, 256));
        if (val.some((x) => x === undefined)) continue;
      } else {
        val = vsPrimitive(v, 2000);
        if (val === undefined) continue;
      }
      fields[k] = val;
      n += 1;
    }
    if (n) out.fields = fields;
  }
  if (!out.scroll && !out.fields) return null;
  if (JSON.stringify(out).length > EMBED_VIEWSTATE_MAX) {
    delete out.fields;
    if (!out.scroll || JSON.stringify(out).length > EMBED_VIEWSTATE_MAX) return null;
  }
  return out;
}

/** The view state of tab object `tab` (`normalizeEmbedViewState()` over its scroll positions and fields). */
export function tabViewState(tab) {
  if (!tab || typeof tab !== "object") return null;
  return normalizeEmbedViewState({ scroll: tab.scrollPositions, fields: tab });
}

/** True when two normalized view states are the same. */
export function sameViewState(a, b) {
  return JSON.stringify(a || null) === JSON.stringify(b || null);
}
