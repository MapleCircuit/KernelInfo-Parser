/**
 * shortcuts.js - Keyboard shortcut registry (webapp/API.md §5.31).
 *
 * One place that knows every shortcut: what it does, where it applies and what it is bound to.
 *
 *   shortcuts.register({ id: "palette.open", keys: ["Mod+K", "Mod+P"], scope: "global",
 *                        description: "Search files, symbols and Kconfig", handler: (e, ctx) => ... });
 *
 * - One capture-phase `keydown` listener on the document dispatches every registered handler.
 * - `Mod` is Ctrl on Linux/Windows and Cmd (⌘) on macOS.
 * - Scopes: the open modal ("modal"), then the active tab's type ("code", "kconfig", "diff", ...),
 *   then "global". Narrower scopes win; a modal hides the tab scope.
 * - Text fields (input, textarea, select, contenteditable) are skipped unless `allowInInputs`.
 * - `docOnly` entries document keys handled locally (TUI, pickers, palette navigation): listed by the
 *   overlay (G09), never dispatched here.
 * - User overrides live in localStorage["kip-shortcuts"] as {id: [keys]}; a change emits
 *   `shortcuts:changed` on the document.
 *
 * The chord helpers are pure and DOM-free (webapp/frontend/tests/shortcuts.test.mjs).
 */

export const STORAGE_KEY = "kip-shortcuts";
const MODIFIER_ORDER = ["Mod", "Ctrl", "Meta", "Alt", "Shift"];
const NAMED_KEYS = {
  esc: "Escape", escape: "Escape", enter: "Enter", return: "Enter", space: "Space", " ": "Space",
  tab: "Tab", backspace: "Backspace", delete: "Delete", del: "Delete", insert: "Insert",
  up: "ArrowUp", down: "ArrowDown", left: "ArrowLeft", right: "ArrowRight",
  arrowup: "ArrowUp", arrowdown: "ArrowDown", arrowleft: "ArrowLeft", arrowright: "ArrowRight",
  home: "Home", end: "End", pageup: "PageUp", pagedown: "PageDown", plus: "+",
};
const MODIFIER_NAMES = {
  mod: "Mod", cmdorctrl: "Mod", ctrl: "Ctrl", control: "Ctrl", meta: "Meta", cmd: "Meta", command: "Meta",
  super: "Meta", alt: "Alt", option: "Alt", opt: "Alt", shift: "Shift",
};
const MAC_SYMBOLS = { Mod: "⌘", Meta: "⌘", Ctrl: "⌃", Alt: "⌥", Shift: "⇧" };
const KEY_LABELS = {
  ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→", Escape: "Esc", Space: "Space",
};

export function detectMac(nav = globalThis.navigator) {
  const p = (nav && (nav.userAgentData?.platform || nav.platform || nav.userAgent)) || "";
  return /mac|iphone|ipad|ipod/i.test(p);
}

function normalizeKeyName(raw) {
  if (raw == null) return "";
  const k = String(raw);
  if (k === " ") return "Space";
  if (k.length === 1) return /[a-z]/i.test(k) ? k.toUpperCase() : k;
  const named = NAMED_KEYS[k.toLowerCase()];
  if (named) return named;
  if (/^f\d{1,2}$/i.test(k)) return k.toUpperCase();
  return k.charAt(0).toUpperCase() + k.slice(1);
}

/** Printable keys other than letters already carry Shift in their character ("?" is Shift+/). */
function shiftIsImplied(key) {
  return key.length === 1 && !/[A-Z0-9]/.test(key) && key !== "+";
}

/**
 * Parse "Mod+Shift+P", "?", "F3", "Shift+F12", "Ctrl++" into {key, mods: Set}. Returns null for an
 * empty or malformed chord.
 */
export function parseChord(text) {
  if (typeof text !== "string") return null;
  const s = text.trim();
  if (!s) return null;
  // "+" as the key itself: "Ctrl++" or "+"
  let parts;
  if (s === "+") parts = ["+"];
  else if (s.endsWith("++")) parts = [...s.slice(0, -2).split("+"), "+"];
  else parts = s.split("+");
  if (parts.some((p) => p === "")) return null;
  const mods = new Set();
  let key = "";
  for (const [i, part] of parts.entries()) {
    const mod = MODIFIER_NAMES[part.toLowerCase()];
    if (mod && i < parts.length - 1) {
      mods.add(mod);
      continue;
    }
    if (i !== parts.length - 1) return null;
    key = normalizeKeyName(part);
  }
  if (!key) return null;
  if (shiftIsImplied(key)) mods.delete("Shift");
  return { key, mods };
}

/** Canonical text of a chord: modifiers in a fixed order, then the key ("Mod+Shift+P"). */
export function normalizeChord(text) {
  const c = parseChord(text);
  if (!c) return "";
  return [...MODIFIER_ORDER.filter((m) => c.mods.has(m)), c.key].join("+");
}

/**
 * Canonical chord of a keydown event. On macOS Cmd is "Mod" and Control is "Ctrl"; elsewhere Control
 * is "Mod" and the Windows/Super key is "Meta".
 */
export function chordFromEvent(e, mac = detectMac()) {
  const raw = e.key;
  if (!raw || ["Control", "Shift", "Alt", "Meta", "OS", "Dead", "Unidentified"].includes(raw)) return "";
  const key = normalizeKeyName(raw);
  const mods = [];
  if (mac ? e.metaKey : e.ctrlKey) mods.push("Mod");
  if (mac && e.ctrlKey) mods.push("Ctrl");
  if (!mac && e.metaKey) mods.push("Meta");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey && !shiftIsImplied(key)) mods.push("Shift");
  return [...mods, key].join("+");
}

/** Human label of a chord for the platform: "Ctrl+Shift+P" or "⌘⇧P". */
export function formatChord(text, mac = detectMac()) {
  const c = parseChord(text);
  if (!c) return String(text || "");
  const mods = MODIFIER_ORDER.filter((m) => c.mods.has(m));
  const keyLabel = KEY_LABELS[c.key] || c.key;
  if (mac) return mods.map((m) => MAC_SYMBOLS[m] || m).join("") + keyLabel;
  return [...mods.map((m) => (m === "Mod" ? "Ctrl" : m)), keyLabel].join("+");
}

/** Scope chain for a dispatch context, narrowest first. */
export function scopeChain(ctx = {}) {
  if (ctx.modal) return ["modal", "global"];
  const chain = [];
  if (ctx.scope) chain.push(ctx.scope);
  if (ctx.tabType && ctx.tabType !== ctx.scope) chain.push(ctx.tabType);
  chain.push("global");
  return chain;
}

/** True when the event target is a text field (where plain keys type text). */
export function isTextTarget(target) {
  if (!target) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  const type = String(target.type || "text").toLowerCase();
  return !["button", "checkbox", "radio", "submit", "reset", "range", "color", "file", "image"].includes(type);
}

/** Merge user overrides ({id: [keys]}) into definitions: the keys each id is bound to. */
export function mergeOverrides(defs, overrides = {}) {
  const out = new Map();
  for (const def of defs) {
    const custom = overrides && Object.prototype.hasOwnProperty.call(overrides, def.id) ? overrides[def.id] : null;
    const keys = Array.isArray(custom) ? custom : def.keys;
    out.set(def.id, (keys || []).map(normalizeChord).filter(Boolean));
  }
  return out;
}

/**
 * Duplicate bindings: [{scope, chord, ids}] for every chord bound to more than one dispatched entry in the
 * same scope. Entries whose `when` makes them exclusive can opt out with `shareKeys: true`.
 */
export function findConflicts(defs, bindings) {
  const seen = new Map();
  for (const def of defs) {
    if (def.docOnly || def.shareKeys) continue;
    for (const chord of bindings.get(def.id) || []) {
      const k = `${def.scope}\u0000${chord}`;
      if (!seen.has(k)) seen.set(k, []);
      seen.get(k).push(def.id);
    }
  }
  const out = [];
  for (const [k, ids] of seen) {
    if (ids.length < 2) continue;
    const [scope, chord] = k.split("\u0000");
    out.push({ scope, chord, ids });
  }
  return out;
}

export class ShortcutRegistry {
  constructor({ storage = null, mac = null, eventTarget = null } = {}) {
    this.defs = new Map();
    this.storage = storage;
    this.mac = mac;
    this.eventTarget = eventTarget;
    this.contextProvider = () => ({});
    this._overrides = null;
    this._installed = false;
    this._onKeyDown = (e) => this.dispatch(e);
  }

  get isMac() {
    if (this.mac === null) this.mac = detectMac();
    return this.mac;
  }

  _store() {
    if (this.storage) return this.storage;
    try {
      return globalThis.localStorage || null;
    } catch {
      return null;
    }
  }

  overrides() {
    if (this._overrides) return this._overrides;
    let parsed = {};
    try {
      const raw = this._store()?.getItem(STORAGE_KEY);
      parsed = raw ? JSON.parse(raw) : {};
    } catch {
      parsed = {};
    }
    this._overrides = parsed && typeof parsed === "object" ? parsed : {};
    return this._overrides;
  }

  _saveOverrides(next) {
    this._overrides = next;
    try {
      const store = this._store();
      if (store) {
        if (Object.keys(next).length) store.setItem(STORAGE_KEY, JSON.stringify(next));
        else store.removeItem(STORAGE_KEY);
      }
    } catch {
      // storage unavailable (private mode): the override lasts for this page only
    }
    this._emit();
  }

  _emit() {
    const target = this.eventTarget || globalThis.document;
    if (target?.dispatchEvent && typeof CustomEvent === "function") {
      target.dispatchEvent(new CustomEvent("shortcuts:changed", { detail: { overrides: { ...this.overrides() } } }));
    }
  }

  /** Register (or replace) one entry. Returns an unregister function. */
  register(def) {
    if (!def || !def.id) throw new Error("shortcut needs an id");
    const entry = {
      scope: "global",
      keys: [],
      description: "",
      when: null,
      handler: null,
      allowInInputs: false,
      docOnly: false,
      rebindable: !def.docOnly,
      shareKeys: false,
      group: null,
      ...def,
    };
    entry.keys = (entry.keys || []).map(normalizeChord).filter(Boolean);
    this.defs.set(entry.id, entry);
    return () => {
      if (this.defs.get(entry.id) === entry) this.defs.delete(entry.id);
    };
  }

  registerAll(defs) {
    const offs = defs.map((d) => this.register(d));
    return () => offs.forEach((off) => off());
  }

  get(id) {
    return this.defs.get(id) || null;
  }

  bindings() {
    return mergeOverrides([...this.defs.values()], this.overrides());
  }

  keysFor(id) {
    return this.bindings().get(id) || [];
  }

  /** Display label of an entry's first binding ("" when unbound or unknown). */
  label(id) {
    const keys = this.keysFor(id);
    return keys.length ? formatChord(keys[0], this.isMac) : "";
  }

  /** Every entry with its effective keys: [{id, scope, description, keys, defaultKeys, docOnly, rebindable, customized}]. */
  list() {
    const bindings = this.bindings();
    const overrides = this.overrides();
    return [...this.defs.values()].map((d) => ({
      id: d.id,
      scope: d.scope,
      group: d.group || d.scope,
      description: d.description,
      keys: bindings.get(d.id) || [],
      defaultKeys: d.keys,
      docOnly: d.docOnly,
      rebindable: d.rebindable,
      customized: Object.prototype.hasOwnProperty.call(overrides, d.id),
    }));
  }

  conflicts(extraOverrides = null) {
    const merged = extraOverrides ? { ...this.overrides(), ...extraOverrides } : this.overrides();
    return findConflicts([...this.defs.values()], mergeOverrides([...this.defs.values()], merged));
  }

  /** Bind `id` to `keys`; refused (returns the conflicts) when another entry of its scope uses one of them. */
  setOverride(id, keys) {
    const def = this.defs.get(id);
    if (!def || !def.rebindable) return { ok: false, conflicts: [], error: "not rebindable" };
    const normalized = (keys || []).map(normalizeChord).filter(Boolean);
    const conflicts = this.conflicts({ [id]: normalized }).filter((c) => c.ids.includes(id));
    if (conflicts.length) return { ok: false, conflicts };
    const next = { ...this.overrides() };
    const same = normalized.length === def.keys.length && normalized.every((k, i) => k === def.keys[i]);
    if (same) delete next[id];
    else next[id] = normalized;
    this._saveOverrides(next);
    return { ok: true, conflicts: [] };
  }

  resetOverride(id) {
    const next = { ...this.overrides() };
    delete next[id];
    this._saveOverrides(next);
  }

  resetAll() {
    this._saveOverrides({});
  }

  /** Function returning the dispatch context: {tabType, modal, scope}. Set by the app. */
  setContextProvider(fn) {
    this.contextProvider = fn || (() => ({}));
  }

  /** Entry that handles `chord` in `ctx` (narrowest scope first), or null. */
  match(chord, ctx = {}, target = null) {
    if (!chord) return null;
    const bindings = this.bindings();
    const textTarget = isTextTarget(target);
    for (const scope of scopeChain(ctx)) {
      for (const def of this.defs.values()) {
        if (def.docOnly || !def.handler || def.scope !== scope) continue;
        if (!(bindings.get(def.id) || []).includes(chord)) continue;
        if (textTarget && !def.allowInInputs) continue;
        try {
          if (def.when && !def.when(ctx)) continue;
        } catch {
          continue;
        }
        return def;
      }
    }
    return null;
  }

  /** While true (a chord is being captured for rebinding), nothing is dispatched. */
  suspend(on = true) {
    this.suspended = Boolean(on);
  }

  dispatch(e) {
    if (this.suspended || e.defaultPrevented || e.isComposing) return false;
    const chord = chordFromEvent(e, this.isMac);
    if (!chord) return false;
    let ctx = {};
    try {
      ctx = this.contextProvider() || {};
    } catch {
      ctx = {};
    }
    const def = this.match(chord, ctx, e.target);
    if (!def) return false;
    const result = def.handler(e, ctx);
    if (result !== false) {
      e.preventDefault();
      e.stopPropagation();
    }
    return result !== false;
  }

  install(target = globalThis.document) {
    if (this._installed || !target) return;
    target.addEventListener("keydown", this._onKeyDown, true);
    this._installed = true;
  }
}

export const shortcuts = new ShortcutRegistry();

// ---------------------------------------------------------------- overlay model (G09, §5.31)

export const SCOPE_TITLES = {
  global: "Global", code: "Code view", "code-find": "Find bar", kconfig: "KConfig", "kconfig-tui": "KConfig TUI",
  diff: "Compare", mail: "Mail", palette: "Command palette", picker: "Revision picker", modal: "Dialogs",
};
const SCOPE_ORDER = Object.keys(SCOPE_TITLES);

/**
 * Entries of `list()` grouped for display, filtered by `query` (matched against the description, the id
 * and the key labels): [{scope, title, entries: [{...entry, labels}]}].
 */
export function groupShortcuts(list, query = "", mac = false) {
  const q = String(query || "").trim().toLowerCase();
  const groups = new Map();
  for (const e of list) {
    const labels = (e.keys || []).map((k) => formatChord(k, mac));
    const hay = `${e.description} ${e.id} ${labels.join(" ")} ${(e.keys || []).join(" ")}`.toLowerCase();
    if (q && !hay.includes(q)) continue;
    const scope = e.scope || "global";
    if (!groups.has(scope)) groups.set(scope, []);
    groups.get(scope).push({ ...e, labels });
  }
  const rank = (s) => (SCOPE_ORDER.includes(s) ? SCOPE_ORDER.indexOf(s) : SCOPE_ORDER.length);
  return [...groups.entries()]
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
    .map(([scope, entries]) => ({ scope, title: SCOPE_TITLES[scope] || scope, entries }));
}

/**
 * One keydown while capturing a chord for rebinding: {state: "waiting"} for a lone modifier,
 * {state: "cancelled"} for Escape, {state: "captured", chord} otherwise.
 */
export function captureStep(e, mac = false) {
  if (e.key === "Escape" && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) return { state: "cancelled" };
  const chord = chordFromEvent(e, mac);
  if (!chord) return { state: "waiting" };
  return { state: "captured", chord };
}
