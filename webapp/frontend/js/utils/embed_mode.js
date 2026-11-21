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
