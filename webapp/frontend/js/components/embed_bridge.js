/**
 * embed_bridge.js - The embed shell's side of a NodeMap tab node (`webapp/API_FRONTEND.md` §5.3.4).
 *
 * An embed shell opened by a tab node (`?embed=1&embedNode=<node id>`) asks the NodeMap around it for the
 * node's saved view state before it opens its tab (`hello` → `restore`, bounded wait), puts it on the tab
 * (view-local fields, `scrollPositions` for `components/scroll_memory.js`), and from then on reports its
 * address (`location.hash`), tab title and view state (`state`, debounced, only when it changed) on
 * navigation, tab state changes, scrolling and input. Messages go to `window.parent` at this origin only
 * and are accepted from it only (`utils/embed_mode.js` `EMBED_MSG`). Whether a report is kept is the
 * NodeMap's decision (Keep changes vs Frozen, `views/nodemap/nodemap_tab_sync.js`).
 */
import { EMBED_MSG, embedNodeToken, normalizeEmbedViewState, tabViewState } from "../utils/embed_mode.js";

export const EMBED_REPORT_DELAY_MS = 300;
export const EMBED_HELLO_TIMEOUT_MS = 600;
const HASH_MAX = 4096;
const TITLE_MAX = 512;

export class EmbedBridge {
  /**
   * @param {{win?: Window, loc?: Location, doc?: Document, state: object, render?: () => void,
   *          delay?: number, helloTimeout?: number}} opts  `state` the app state (getActiveTab(), subscribe()),
   *          `render` re-renders the panes after a restore
   */
  constructor({ win = globalThis.window, loc = globalThis.location, doc = globalThis.document, state, render = null, delay = EMBED_REPORT_DELAY_MS, helloTimeout = EMBED_HELLO_TIMEOUT_MS } = {}) {
    this.win = win;
    this.loc = loc;
    this.doc = doc;
    this.state = state;
    this.render = render;
    this.delay = delay;
    this.helloTimeout = helloTimeout;
    this.node = embedNodeToken(loc);
    this.parent = win && win.parent && win.parent !== win ? win.parent : null;
    this.timer = null;
    this.lastSent = "";
    this.offs = [];
    this.started = false;
  }

  /** True when this shell belongs to a NodeMap tab node it can report to. */
  get enabled() {
    return Boolean(this.node && this.parent && this.loc?.origin);
  }

  /** The saved view state of the node (null after `helloTimeout`, or without a NodeMap around). */
  handshake() {
    if (!this.enabled) return Promise.resolve(null);
    return new Promise((resolve) => {
      let done = false;
      let timer = null;
      const onMessage = (e) => {
        if (e.origin !== this.loc.origin || e.source !== this.parent) return;
        const msg = e.data;
        if (!msg || msg.type !== EMBED_MSG.restore || msg.node !== this.node) return;
        finish(normalizeEmbedViewState(msg.viewState));
      };
      const finish = (vs) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        this.win.removeEventListener("message", onMessage);
        resolve(vs);
      };
      this.win.addEventListener("message", onMessage);
      timer = setTimeout(() => finish(null), this.helloTimeout);
      try {
        this.parent.postMessage({ type: EMBED_MSG.hello, node: this.node }, this.loc.origin);
      } catch {
        finish(null);
      }
    });
  }

  /** Put view state `vs` on the active tab and render it again (its scroll memory restores the positions). */
  applyRestore(vs) {
    const tab = this.state?.getActiveTab?.();
    const clean = normalizeEmbedViewState(vs);
    if (!tab || !clean) return false;
    for (const [k, v] of Object.entries(clean.fields || {})) {
      // a field of the tab's own data only (never one of its methods or inherited properties)
      if (typeof tab[k] === "function" || (k in tab && !Object.prototype.hasOwnProperty.call(tab, k))) continue;
      tab[k] = Array.isArray(v) ? [...v] : v;
    }
    if (clean.scroll) tab.scrollPositions = { ...clean.scroll };
    this.render?.();
    return true;
  }

  /** What this shell shows now: `{hash, title, viewState}`. */
  snapshot() {
    const tab = this.state?.getActiveTab?.() || null;
    return {
      hash: String(this.loc?.hash || "").replace(/^#/, "").slice(0, HASH_MAX),
      title: String(tab?.title || "").slice(0, TITLE_MAX),
      viewState: tabViewState(tab),
    };
  }

  /** Report the current state (after `delay`, or now with `now`); unchanged state is not sent again. */
  report(now = false) {
    if (!this.enabled) return;
    if (!now) {
      if (this.timer) return;
      this.timer = setTimeout(() => {
        this.timer = null;
        this.report(true);
      }, this.delay);
      return;
    }
    clearTimeout(this.timer);
    this.timer = null;
    const snap = this.snapshot();
    if (!snap.hash) return;
    const json = JSON.stringify(snap);
    if (json === this.lastSent) return;
    this.lastSent = json;
    try {
      this.parent.postMessage({ type: EMBED_MSG.state, node: this.node, ...snap }, this.loc.origin);
    } catch {
      // the NodeMap went away
    }
  }

  /** Start reporting (and report the state the shell opened with). */
  start() {
    if (!this.enabled || this.started) return;
    this.started = true;
    const later = () => this.report();
    const listen = (target, type, fn, opts) => {
      if (!target?.addEventListener) return;
      target.addEventListener(type, fn, opts);
      this.offs.push(() => target.removeEventListener(type, fn, opts));
    };
    listen(this.win, "hashchange", later);
    listen(this.win, "popstate", later);
    listen(this.win, "pagehide", () => this.report(true));
    listen(this.doc, "scroll", later, { capture: true, passive: true });
    listen(this.doc, "input", later, true);
    listen(this.doc, "change", later, true);
    for (const ev of ["tab:state", "tabs:changed"]) {
      const off = this.state?.subscribe?.(ev, later);
      if (typeof off === "function") this.offs.push(off);
    }
    this.report();
  }

  dispose() {
    clearTimeout(this.timer);
    this.timer = null;
    for (const off of this.offs) off();
    this.offs = [];
    this.started = false;
  }
}
