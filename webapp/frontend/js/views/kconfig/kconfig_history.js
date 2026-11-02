/**
 * kconfig_history.js - Undo/redo of a KConfig tab's configuration (Ctrl+Z / Ctrl+Shift+Z, Ctrl+Y).
 *
 * A configuration state is a snapshot `{raw, engine, engineOn}`: the tab's values (`rawValues`),
 * the engine's own state (`KconfigEngine.captureState()`: user values, choice values and defaults,
 * choice priority) and whether the tab runs its engine. Only the *difference* between consecutive
 * states is kept in memory: each entry maps every key that changed to its `[before, after]` pair
 * (`undefined` = absent), so a change of one option costs a few entries however large the
 * configuration is. The current state (`base`) is the only full snapshot held.
 *
 * Undo applies the `before` side of the newest entry to `base`, redo the `after` side; the result
 * is exactly the state the tab had, which the view then puts back without re-resolving it.
 */

const LIMIT = 200;
const ENGINE_MAPS = ["user", "choiceUser", "choiceDefTri", "choiceOrder"];

const same = (a, b) => (Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((v, i) => v === b[i]) : a === b);

/** `{key: [before, after]}` for every key whose value differs between two Maps. */
function diffMaps(a, b) {
  const out = [];
  for (const [k, v] of a) if (!b.has(k) || !same(v, b.get(k))) out.push([k, v, b.get(k)]);
  for (const [k, v] of b) if (!a.has(k)) out.push([k, undefined, v]);
  return out;
}

function patchMap(map, changes, side) {
  const next = new Map(map);
  for (const change of changes) {
    const v = change[side];
    if (v === undefined) next.delete(change[0]);
    else next.set(change[0], Array.isArray(v) ? [...v] : v);
  }
  return next;
}

const toMap = (obj) => new Map(Object.entries(obj || {}).map(([k, v]) => [k, String(v)]));

export class KconfigHistory {
  constructor(limit = LIMIT) {
    this.limit = limit;
    this.clear("");
  }

  /** Forget every entry; `key` names the model the history belongs to. */
  clear(key, base = null) {
    this.key = key;
    this.base = base;
    this.undoStack = [];
    this.redoStack = [];
  }

  /** Take `snap` as the current state, keeping the difference from the previous one as an entry. */
  record(snap) {
    const prev = this.base;
    this.base = snap;
    if (!prev) return null;
    const entry = { raw: diffMaps(toMap(prev.raw), toMap(snap.raw)), engine: {}, engineOn: null };
    let empty = !entry.raw.length;
    const engineDiffable = prev.engine && snap.engine;
    for (const name of ENGINE_MAPS) {
      entry.engine[name] = engineDiffable ? diffMaps(prev.engine[name], snap.engine[name]) : [];
      if (entry.engine[name].length) empty = false;
    }
    if (!engineDiffable && (prev.engine || snap.engine)) {
      entry.engineFull = [prev.engine, snap.engine];
      empty = false;
    }
    if (prev.engineOn !== snap.engineOn) {
      entry.engineOn = [prev.engineOn, snap.engineOn];
      empty = false;
    }
    if (empty) return null;
    this.undoStack.push(entry);
    if (this.undoStack.length > this.limit) this.undoStack.shift();
    this.redoStack = [];
    return entry;
  }

  /** State before the newest change, or null; the entry moves to the redo stack. */
  undo() {
    return this._step(this.undoStack, this.redoStack, 1);
  }

  /** State after the last undone change, or null; the entry moves back to the undo stack. */
  redo() {
    return this._step(this.redoStack, this.undoStack, 2);
  }

  _step(from, to, side) {
    const entry = from.pop();
    if (!entry || !this.base) return null;
    const base = this.base;
    const raw = Object.fromEntries(patchMap(toMap(base.raw), entry.raw, side));
    let engine;
    if (entry.engineFull) engine = entry.engineFull[side - 1];
    else if (base.engine) engine = Object.fromEntries(ENGINE_MAPS.map((n) => [n, patchMap(base.engine[n], entry.engine[n], side)]));
    else engine = null;
    const engineOn = entry.engineOn ? entry.engineOn[side - 1] : base.engineOn;
    this.base = { raw, engine, engineOn };
    to.push(entry);
    return { snap: this.base, entry };
  }

  /** Short description of an entry for a toast: the option it changed, or how many. */
  static describe(entry, side) {
    const parts = [];
    if (entry.raw.length === 1) {
      const [name, before, after] = entry.raw[0];
      const v = side === 1 ? before : after;
      parts.push(`CONFIG_${name}=${v === undefined ? "(not set)" : v || '""'}`);
    } else if (entry.raw.length) {
      parts.push(`${entry.raw.length} values`);
    }
    if (entry.engineOn) parts.push(`engine ${entry.engineOn[side - 1] ? "on" : "off"}`);
    return parts.join(", ") || "engine state";
  }
}
