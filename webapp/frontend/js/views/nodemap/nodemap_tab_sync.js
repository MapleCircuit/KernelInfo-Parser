/**
 * nodemap_tab_sync.js - Live state of embedded `tab` nodes (`webapp/API_FRONTEND.md` §5.3.4).
 *
 * The NodeMap side of the embed shell bridge (`utils/embed_mode.js` `EMBED_MSG`, `components/embed_bridge.js`):
 * a shell that starts says `hello` and gets the node's saved `viewState` back (`restore`); afterwards it
 * reports its address, title and view state (`state`, debounced). A node in **Keep changes** mode
 * (`data.frozen === false`, the default) writes each report into `data.hash` / `data.viewState` and saves
 * the canvas, without an undo step and without reloading its own frame; a **Frozen** node ignores the
 * reports (they only mark it dirty) and goes back to its frozen state on Reset or when the NodeMap tab is
 * shown again. Every message must come from this origin and from the iframe of the node it names.
 */
import { EMBED_MSG, EMBED_NODE_RE, normalizeEmbedViewState, sameViewState } from "../../utils/embed_mode.js";
import * as M from "./canvas_model.js";

const HASH_MAX = 4096;
const TITLE_MAX = 512;

/**
 * Live record of a tab node's frame: `src` the hash it was loaded with, `hash` / `viewState` / `title` its
 * latest report (`reported`), `dirty` (frozen: it shows something else than the frozen state) and
 * `epoch` (Reset count: part of the body key, so a Reset reloads the frame).
 */
export function liveOf(ctrl, id, create = true) {
  if (!ctrl.tabLive) ctrl.tabLive = new Map();
  let rec = ctrl.tabLive.get(id) || null;
  if (!rec && create) {
    rec = { src: null, hash: null, viewState: null, title: "", reported: false, dirty: false, epoch: 0 };
    ctrl.tabLive.set(id, rec);
  }
  return rec;
}

/** The mounted iframe of tab node `id`, or null. */
export function tabFrameOf(ctrl, id) {
  return ctrl.renderer?.element?.(id)?.querySelector?.("iframe.nm-iframe") || null;
}

/**
 * What makes a tab node's body (its iframe) stale: its embedded session, its address and its Reset count.
 * A hash the frame itself reported (written back in Keep changes mode) keys as the hash the frame was
 * loaded with, so the frame's own navigation never reloads it; any other hash (undo, import, an edit)
 * does. The mode and the view state never reload it.
 */
export function tabKeyData(ctrl, node) {
  const d = node.data || {};
  const live = ctrl.tabLive?.get(node.id) || null;
  const own = live && live.reported && live.src !== null && d.hash === live.hash;
  return { embedSessionId: d.embedSessionId || "", hash: own ? live.src : d.hash, epoch: live?.epoch || 0 };
}

/** A new frame of node `node` is mounted (with `node.data.hash`): nothing is reported for it yet. */
export function frameMounted(ctrl, node) {
  const live = liveOf(ctrl, node.id);
  Object.assign(live, { src: node.data.hash, hash: null, viewState: null, title: "", reported: false, dirty: false });
  return live;
}

/** The frame of node `id` is torn down (rebuilt, or the body shows no frame): its reports are gone with it. */
export function frameDisposed(ctrl, id) {
  const live = ctrl.tabLive?.get(id);
  if (live) Object.assign(live, { src: null, hash: null, viewState: null, title: "", reported: false, dirty: false });
}

/** Answer the `hello`s that came while the stored canvas was loading (their nodes now hold the stored state). */
export function flushPendingHellos(ctrl) {
  const queued = ctrl._pendingHellos || [];
  ctrl._pendingHellos = [];
  for (const e of queued) onTabFrameMessage(ctrl, e);
}

function updateChrome(ctrl, node) {
  const inspector = ctrl.selected?.size === 1 && ctrl.selected.has(node.id);
  ctrl.commit?.({ save: false, inspector });
}

/**
 * Handle a `hello` / `state` message of an embed shell (`window` `message` event). Returns true when
 * it was one for a tab node of this canvas, from that node's own iframe.
 */
export function onTabFrameMessage(ctrl, e) {
  if (!e || typeof location === "undefined" || e.origin !== location.origin) return false;
  const msg = e.data;
  if (!msg || typeof msg !== "object" || (msg.type !== EMBED_MSG.hello && msg.type !== EMBED_MSG.state)) return false;
  if (typeof msg.node !== "string" || !EMBED_NODE_RE.test(msg.node)) return false;
  const node = ctrl.canvas?.nodes?.find((n) => n.id === msg.node) || null;
  if (!node || node.type !== "tab") return false;
  const frame = tabFrameOf(ctrl, node.id);
  if (!frame || !e.source || e.source !== frame.contentWindow) return false;
  // The stored canvas is still loading: its saved view state is not here yet, and a report written now
  // would be replaced by it (hellos are answered once it is in, reports wait for the next one)
  if (ctrl._storeLoading) {
    if (msg.type === EMBED_MSG.hello) (ctrl._pendingHellos = ctrl._pendingHellos || []).push(e);
    return true;
  }
  const live = liveOf(ctrl, node.id);
  if (msg.type === EMBED_MSG.hello) {
    try {
      frame.contentWindow.postMessage({ type: EMBED_MSG.restore, node: node.id, viewState: node.data.viewState || null }, location.origin);
    } catch {
      // the frame went away meanwhile
    }
    return true;
  }
  const hash = typeof msg.hash === "string" ? msg.hash.replace(/^#/, "").slice(0, HASH_MAX) : "";
  if (!hash) return true;
  live.hash = hash;
  live.viewState = normalizeEmbedViewState(msg.viewState);
  live.title = typeof msg.title === "string" ? msg.title.trim().slice(0, TITLE_MAX) : "";
  live.reported = true;
  if (node.data.frozen) {
    const dirty = hash !== node.data.hash || !sameViewState(live.viewState, node.data.viewState);
    if (dirty !== live.dirty) {
      live.dirty = dirty;
      updateChrome(ctrl, node);
    }
    return true;
  }
  live.dirty = false;
  writeLiveState(ctrl, node, live);
  return true;
}

/**
 * Keep changes: the frame's latest report becomes the node's `hash` / `viewState` (and its title unless
 * the node was renamed). Saved like any edit but not recorded as an undo step. Returns true when
 * something changed.
 */
export function writeLiveState(ctrl, node, live = ctrl.tabLive?.get(node.id)) {
  if (!live?.reported || !live.hash) return false;
  const d = node.data;
  const hashChanged = d.hash !== live.hash;
  const vsChanged = !sameViewState(d.viewState, live.viewState);
  const titleChanged = Boolean(live.title) && !node.customTitle && node.title !== live.title;
  if (!hashChanged && !vsChanged && !titleChanged) return false;
  d.hash = live.hash;
  d.viewState = live.viewState;
  const tabType = M.tabTypeOfHash(live.hash);
  if (tabType) d.tabType = tabType.slice(0, 32);
  if (titleChanged) {
    node.title = live.title;
    const titleEl = ctrl.renderer?.element?.(node.id)?.querySelector?.(".nm-title");
    if (titleEl && !titleEl.querySelector?.("input")) titleEl.textContent = M.displayTitle(node);
  }
  const focusInInspector = typeof document !== "undefined" && ctrl.inspectorEl?.contains?.(document.activeElement);
  if ((hashChanged || titleChanged) && ctrl.selected?.size === 1 && ctrl.selected.has(node.id) && !focusInInspector) ctrl.renderInspector?.();
  ctrl.scheduleSave?.();
  return true;
}

/**
 * Switch tab nodes `ids` to Frozen (`frozen`) or Keep changes. Either way the frame's current state
 * (its latest report) becomes the node's: freezing snapshots what the tab shows now, unfreezing keeps it
 * from here on. One undo step; returns how many nodes changed.
 */
export function setTabsFrozen(ctrl, ids, frozen) {
  const want = Boolean(frozen);
  let changed = 0;
  for (const id of ids) {
    const node = ctrl.byId?.(id);
    if (!node || node.type !== "tab" || Boolean(node.data.frozen) === want) continue;
    const live = ctrl.tabLive?.get(id) || null;
    if (live?.reported && live.hash) {
      node.data.hash = live.hash;
      node.data.viewState = live.viewState;
      const tabType = M.tabTypeOfHash(live.hash);
      if (tabType) node.data.tabType = tabType.slice(0, 32);
      if (live.title && !node.customTitle) node.title = live.title;
    }
    node.data.frozen = want;
    if (live) live.dirty = false;
    changed += 1;
  }
  if (changed) ctrl.commit?.();
  return changed;
}

/** Reload tab node `id` at its saved (frozen) state: a new frame, restored with `data.viewState`. */
export function resetTabNode(ctrl, id) {
  const node = ctrl.byId?.(id);
  if (!node || node.type !== "tab") return false;
  const live = liveOf(ctrl, id);
  live.epoch += 1;
  live.dirty = false;
  live.reported = false;
  ctrl.renderer?.invalidate?.(id);
  ctrl.commit?.({ save: false });
  return true;
}

/** Reset every frozen tab node whose frame wandered off its frozen state (the NodeMap tab is shown again). */
export function resetDirtyFrozenTabs(ctrl) {
  let n = 0;
  for (const node of ctrl.canvas?.nodes || []) {
    if (node.type !== "tab" || !node.data.frozen || !ctrl.tabLive?.get(node.id)?.dirty) continue;
    if (resetTabNode(ctrl, node.id)) n += 1;
  }
  return n;
}

/**
 * Undo / redo keep what a Keep-changes tab currently shows: a snapshot's `hash` / `viewState` of node
 * `fresh` give way to the live ones of `cur` when `cur`'s came from its own frame and `fresh` is not frozen.
 */
export function keepLiveOnRestore(ctrl, cur, fresh) {
  if (cur?.type !== "tab" || fresh?.type !== "tab" || fresh.data?.frozen || cur.data?.frozen) return fresh;
  const live = ctrl.tabLive?.get(cur.id);
  if (!live?.reported || cur.data.hash !== live.hash) return fresh;
  fresh.data = { ...fresh.data, hash: cur.data.hash, tabType: cur.data.tabType, viewState: cur.data.viewState };
  return fresh;
}

/** Recompute `dirty` of the frozen tab nodes (their frozen state may have changed under the frames). */
export function reconcileFrozenTabs(ctrl) {
  for (const node of ctrl.canvas?.nodes || []) {
    if (node.type !== "tab") continue;
    const live = ctrl.tabLive?.get(node.id);
    if (!live) continue;
    live.dirty = Boolean(node.data.frozen && live.reported && (live.hash !== node.data.hash || !sameViewState(live.viewState, node.data.viewState)));
  }
}

/** Whether tab node `id` is frozen and its frame shows something else than the frozen state. */
export function isTabDirty(ctrl, id) {
  return Boolean(ctrl.tabLive?.get(id)?.dirty);
}
