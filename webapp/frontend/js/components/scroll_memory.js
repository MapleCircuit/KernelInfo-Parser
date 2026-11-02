/**
 * scroll_memory.js - Universal per-tab scroll positions (webapp/API.md §5.28).
 *
 * Every scrollable element inside a tab's content area has its position kept on the tab
 * (`tab.scrollPositions`, persisted with the session), and is put back when the tab is rendered
 * again — after switching tabs, re-rendering the panes, or reopening the browser on that session.
 * Views do not opt in: `TabsComponent` attaches this to each pane's content container. A subtree
 * marked `data-scroll-restore="off"` manages its own position (the code view's line viewport).
 */
import { state } from "../state.js";

const OPT_OUT_SELECTOR = "[data-scroll-restore='off']";
// Views fill their content asynchronously (API calls, IndexedDB), so a saved position is applied as
// soon as its element exists and is tall enough, for at most this long after the tab is rendered.
const RESTORE_WINDOW_MS = 15000;
// Once every position is applied, restoration keeps watching this long for a view that re-renders
// its content right after (cached data first, then the server's answer) and would land back at 0.
const SETTLE_MS = 1500;
const POLL_MS = 250;
const SAVE_DELAY_MS = 400;
// A tab remembers this many scrolled elements at most (least recently scrolled dropped first)
const MAX_KEYS = 24;
// Any of these inside the tab means the reader took over: restoration stops at once
const INTENT_EVENTS = ["wheel", "pointerdown", "keydown", "touchstart"];
const KEY_SEP = "|";

let saveTimer = null;

/** Persist the session once scrolling pauses; scroll events fire far too often to save on each. */
export function scheduleScrollSave() {
  if (saveTimer) return;
  saveTimer = setTimeout(() => {
    saveTimer = null;
    state.saveState();
  }, SAVE_DELAY_MS);
}

/** Write a pending save now (the window is going away): saveState() only schedules, flushState() writes. */
function flushScrollSave() {
  if (!saveTimer) return;
  clearTimeout(saveTimer);
  saveTimer = null;
  state.saveState();
  state.flushState();
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", flushScrollSave);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushScrollSave();
  });
}

/** Forget every position a tab remembers (it now shows something else). */
export function clearScrollPositions(tab) {
  if (tab) delete tab.scrollPositions;
}

function segmentOf(el) {
  const cls = el.classList[0];
  return cls ? `${el.tagName.toLowerCase()}.${cls}` : el.tagName.toLowerCase();
}

/**
 * A key that finds the same element again in a fresh render of the same view: the nearest id, then
 * the tag and first class of each step down with its index among siblings sharing them.
 */
function keyOf(el, root) {
  const parts = [];
  let cur = el;
  while (cur && cur !== root) {
    if (cur.id) {
      parts.unshift(`#${cur.id}`);
      return parts.join(KEY_SEP);
    }
    const parent = cur.parentElement;
    if (!parent) return null;
    const seg = segmentOf(cur);
    let n = 0;
    for (const sib of parent.children) {
      if (sib === cur) break;
      if (segmentOf(sib) === seg) n += 1;
    }
    parts.unshift(`${seg}:${n}`);
    cur = parent;
  }
  if (cur !== root) return null;
  parts.unshift("");
  return parts.join(KEY_SEP);
}

function resolveKey(key, root) {
  const parts = key.split(KEY_SEP);
  let cur;
  const head = parts.shift();
  if (head === "") {
    cur = root;
  } else {
    try {
      cur = root.querySelector(`#${CSS.escape(head.slice(1))}`);
    } catch (e) {
      return null;
    }
  }
  for (const part of parts) {
    if (!cur) return null;
    const at = part.lastIndexOf(":");
    const seg = part.slice(0, at);
    let n = Number(part.slice(at + 1));
    let next = null;
    for (const child of cur.children) {
      if (segmentOf(child) !== seg) continue;
      if (n === 0) {
        next = child;
        break;
      }
      n -= 1;
    }
    cur = next;
  }
  return cur || null;
}

/**
 * Remember and restore the scroll positions of the tab rendered into `root`. Called once per
 * render of a pane's content container; the listeners live and die with that container.
 */
export function attachScrollMemory(root, tab) {
  if (!root || !tab) return;

  const saved = { ...(tab.scrollPositions || {}) };
  const keys = Object.keys(saved).filter((k) => Array.isArray(saved[k]));
  let restoring = keys.length > 0;

  root.addEventListener("scroll", (e) => {
    const el = e.target;
    if (!(el instanceof Element) || el.closest(OPT_OUT_SELECTOR)) return;
    const key = keyOf(el, root);
    if (key === null) return;
    // While a saved position is still being put back, its element's scroll events are the view
    // loading (clamped to an emptied container) or the restoration itself, not the reader.
    if (restoring && key in saved) return;
    const top = Math.round(el.scrollTop);
    const left = Math.round(el.scrollLeft);
    const positions = tab.scrollPositions || (tab.scrollPositions = {});
    const prev = positions[key];
    if (prev && prev[0] === top && prev[1] === left) return;
    delete positions[key]; // re-inserted last: most recently scrolled
    if (top || left) positions[key] = [top, left];
    const all = Object.keys(positions);
    for (let i = 0; i < all.length - MAX_KEYS; i += 1) delete positions[all[i]];
    if (!all.length) delete tab.scrollPositions;
    scheduleScrollSave();
  }, true);

  if (!restoring) return;

  const start = Date.now();
  let lastChange = start;
  const applied = new Map(); // key -> element the position was put on

  const finish = () => {
    if (!restoring) return;
    restoring = false;
    observer.disconnect();
    clearInterval(poll);
    INTENT_EVENTS.forEach((t) => root.removeEventListener(t, finish, true));
  };

  const attempt = () => {
    if (!restoring) return;
    const now = Date.now();
    // A re-render of the panes replaced this container; its own render restores from here on
    if (!root.isConnected && now - start > POLL_MS) {
      finish();
      return;
    }
    const expired = now - start > RESTORE_WINDOW_MS;
    for (const key of keys) {
      const el = resolveKey(key, root);
      if (!el || el.closest(OPT_OUT_SELECTOR)) continue;
      const [top, left] = saved[key];
      if (applied.get(key) === el && Math.abs(el.scrollTop - top) <= 1 && Math.abs(el.scrollLeft - left) <= 1) continue;
      const maxTop = el.scrollHeight - el.clientHeight;
      const maxLeft = el.scrollWidth - el.clientWidth;
      // Not loaded far enough yet: wait for more content, unless the window is over
      if ((maxTop + 1 < top || maxLeft + 1 < left) && !expired) continue;
      el.scrollTop = top;
      el.scrollLeft = left;
      applied.set(key, el);
      lastChange = now;
    }
    if (expired || (applied.size === keys.length && now - lastChange > SETTLE_MS)) finish();
  };

  let frame = 0;
  const observer = new MutationObserver(() => {
    lastChange = Date.now();
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      attempt();
    });
  });
  observer.observe(root, { childList: true, subtree: true });
  const poll = setInterval(attempt, POLL_MS);
  INTENT_EVENTS.forEach((t) => root.addEventListener(t, finish, { capture: true, passive: true }));
  requestAnimationFrame(attempt);
}
