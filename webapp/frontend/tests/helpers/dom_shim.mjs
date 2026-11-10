/**
 * dom_shim.mjs - Minimal browser globals so view modules can be imported under Node (node --test).
 *
 * Only what the modules touch while being evaluated (singletons constructed at import time) is
 * provided; tests exercise pure functions and registries, never real rendering. Import this module
 * before any module of webapp/frontend/js that touches window/document at load time.
 */
class FakeEventTarget {
  constructor() { this._l = new Map(); }
  addEventListener(type, fn) { if (!this._l.has(type)) this._l.set(type, new Set()); this._l.get(type).add(fn); }
  removeEventListener(type, fn) { this._l.get(type)?.delete(fn); }
  dispatchEvent(ev) { for (const fn of this._l.get(ev.type) || []) fn(ev); return true; }
}

class FakeElement extends FakeEventTarget {
  constructor(tag = "div") {
    super();
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
    this.innerHTML = "";
    this.textContent = "";
    this.hidden = false;
  }
  appendChild(c) { this.children.push(c); return c; }
  remove() {}
  setAttribute() {}
  getAttribute() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  contains() { return false; }
  getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 }; }
}

if (typeof globalThis.window === "undefined") {
  const doc = new FakeEventTarget();
  Object.assign(doc, {
    hidden: false,
    body: new FakeElement("body"),
    documentElement: new FakeElement("html"),
    head: new FakeElement("head"),
    createElement: (tag) => new FakeElement(tag),
    createTreeWalker: () => ({ nextNode: () => null }),
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    activeElement: null,
  });
  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
    key: (i) => [...store.keys()][i] ?? null,
    get length() { return store.size; },
  };
  const win = new FakeEventTarget();
  Object.assign(globalThis, {
    window: globalThis,
    document: doc,
    localStorage,
    sessionStorage: localStorage,
    location: { hash: "", href: "http://localhost/", origin: "http://localhost", pathname: "/", search: "" },
    history: { state: null, pushState() {}, replaceState() {} },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    NodeFilter: { SHOW_TEXT: 4 },
    BroadcastChannel: class { constructor() {} postMessage() {} close() {} addEventListener() {} },
  });
  globalThis.addEventListener = win.addEventListener.bind(win);
  globalThis.removeEventListener = win.removeEventListener.bind(win);
  if (typeof globalThis.CustomEvent === "undefined") {
    globalThis.CustomEvent = class CustomEvent extends Event {
      constructor(type, init = {}) { super(type); this.detail = init.detail; }
    };
  }
  try {
    Object.defineProperty(globalThis, "navigator", { value: { onLine: true, platform: "Linux", userAgent: "node", serviceWorker: undefined }, configurable: true });
  } catch {
    // Node 21+ defines navigator; the modules only read onLine/platform
  }
}

export { FakeElement, FakeEventTarget };
