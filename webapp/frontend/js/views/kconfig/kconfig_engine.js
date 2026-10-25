/**
 * kconfig_engine.js - Client-side Linux Kconfig engine (scripts/kconfig port).
 *
 * Evaluates the finalized model served by GET /api/kconfig/{version}/tree (built by
 * parser/kconfig_ast/kconfig_model.py) with kconfig's own semantics:
 *  - sym_calc_value / sym_calc_visibility / sym_calc_choice, ranges, modules handling
 *  - sym_set_tristate_value / sym_set_string_value / sym_toggle_tristate_value (only legal changes)
 *  - conf_read (load .config/defconfig), conf_set_all_new_symbols, conf_write (.config) and
 *    conf_write_defconfig (savedefconfig), menu_is_visible
 * It is the twin of parser/kconfig_ast/kconfig_eval.py; both reproduce `make <defconfig>` byte for byte.
 *
 * Expressions: ids into model.exprs (-1 = always true); nodes are ["s", name] | ["c", text] |
 * ["&", a, b] | ["|", a, b] | ["!", a] | ["=", a, b] | ["!=", a, b] | ["<"|"<="|">"|">=", a, b].
 */

const TRI_CHAR = ["n", "m", "y"];
const TRI_OF = { n: 0, m: 1, y: 2 };
const STRING_TYPES = new Set(["string", "int", "hex"]);
const MASK64 = (1n << 64n) - 1n;
const INT64_MAX = (1n << 63n) - 1n;
const INT64_MIN = -(1n << 63n);

const SYM_KEYS = {
  t: "type", p: "prompts", d: "defaults", r: "ranges", s: "selects", i: "implies", rd: "rev_dep",
  im: "implied", dd: "dir_dep", m: "menus", ch: "choice", ic: "is_choice", op: "optional",
  mb: "members", au: "auto", env: "env", any: "allnoconfig_y",
};
const SYM_DEFAULTS = {
  prompts: [], defaults: [], ranges: [], selects: [], implies: [], rev_dep: -1, implied: -1,
  dir_dep: -1, menus: [], members: [],
};
const KIND_NAMES = { 0: "root", 1: "menu", 2: "choice", 3: "config", 4: "menuconfig", 5: "comment", 6: "if" };

/** C strtoll(): whitespace, sign, optional 0x for base 16, stops at the first invalid digit. */
function strtoll(text, base) {
  const m = base === 16
    ? /^\s*([+-]?)(?:0[xX])?([0-9a-fA-F]*)/.exec(text || "")
    : /^\s*([+-]?)([0-9]*)/.exec(text || "");
  if (!m || !m[2]) return 0n;
  let v = BigInt(base === 16 ? `0x${m[2]}` : m[2]);
  if (m[1] === "-") v = -v;
  return v > INT64_MAX ? INT64_MAX : (v < INT64_MIN ? INT64_MIN : v);
}

export function stringValid(type, text) {
  const s = text ?? "";
  if (type === "string") return true;
  if (type === "int") return /^-?(0|[1-9][0-9]*)$/.test(s);
  if (type === "hex") return /^(0[xX])?[0-9a-fA-F]+$/.test(s);
  if (type === "bool" || type === "tristate") return /^[yYmMnN]/.test(s);
  return false;
}

export function escapeConfigString(value) {
  return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

/** Expand the compact transport form (short keys, menus as arrays) into engine objects. */
export function decodeModel(raw) {
  if (!raw || !Array.isArray(raw.menus)) throw new Error("Unsupported Kconfig model format");
  const files = raw.files || [];
  const nodes = raw.menus.map((row, id) => ({
    id,
    parent: row[0],
    kind: KIND_NAMES[row[1]] || "config",
    sym: row[2],
    prompt: row[3],
    pvis: row[4],
    visIf: row[5],
    dep: row[6],
    file: files[row[7]] || "",
    line: row[8],
    lineEnd: row[9],
    hasHelp: Boolean(row[10]),
    children: [],
  }));
  for (const node of nodes) if (node.parent >= 0) nodes[node.parent].children.push(node.id);
  const syms = {};
  for (const [name, compact] of Object.entries(raw.symbols || {})) {
    const s = { ...SYM_DEFAULTS };
    for (const [short, value] of Object.entries(compact)) s[SYM_KEYS[short] || short] = value;
    syms[name] = s;
  }
  return {
    version: raw.version,
    arch: raw.arch,
    srcarch: raw.srcarch,
    env: raw.env || {},
    mainmenu: raw.mainmenu || "",
    configHeader: raw.config_header || "Automatically generated file; DO NOT EDIT.",
    semantics: raw.semantics || {},
    toolchain: raw.toolchain || null,
    modules: raw.modules_sym || "MODULES",
    exprs: raw.exprs || [],
    syms,
    nodes,
    warnings: raw.warnings || [],
  };
}

export class KconfigEngine {
  constructor(model = null) {
    this.model = null;
    this.user = new Map();
    this.choiceUser = new Map();
    this.choiceDefTri = new Map();
    if (model) this.setModel(model);
  }

  setModel(raw) {
    const m = raw && raw.syms ? raw : decodeModel(raw);
    this.model = m;
    this.exprs = m.exprs;
    this.syms = m.syms;
    this.nodes = m.nodes;
    this.modules = m.modules;
    // Revision-specific conf behaviour (detected server-side from its scripts/kconfig sources)
    this.sem = m.semantics || {};
    this.choices = Object.keys(m.syms).filter((n) => m.syms[n].is_choice);
    this._selectors = null;
    this.user.clear();
    this.choiceUser.clear();
    this.choiceDefTri.clear();
    this.invalidate();
  }

  get loaded() {
    return Boolean(this.model);
  }

  /** sym_clear_all_valid. */
  invalidate() {
    this._cur = new Map();
    this._write = new Map();
    this._rawWrite = new Map();
    this._vis = new Map();
    this._rev = new Map();
    this._implied = new Map();
    this._dir = new Map();
    this._sel = new Map();
    this._memo = new Map();
  }

  // ------------------------------------------------------------------ expressions
  leafStr(id) {
    const node = this.exprs[id];
    return node[0] === "c" ? node[1] : this.symStr(node[1]);
  }

  exprTri(id) {
    if (id === null || id === undefined || id < 0) return 2;
    const cached = this._memo.get(id);
    if (cached !== undefined) return cached;
    const node = this.exprs[id];
    let val;
    switch (node[0]) {
      case "s": val = this.symTri(node[1]); break;
      case "c": val = node[1].length === 1 && node[1] in TRI_OF ? TRI_OF[node[1]] : 0; break;
      case "&": val = Math.min(this.exprTri(node[1]), this.exprTri(node[2])); break;
      case "|": val = Math.max(this.exprTri(node[1]), this.exprTri(node[2])); break;
      case "!": val = 2 - this.exprTri(node[1]); break;
      case "=": val = this.leafStr(node[1]) === this.leafStr(node[2]) ? 2 : 0; break;
      case "!=": val = this.leafStr(node[1]) !== this.leafStr(node[2]) ? 2 : 0; break;
      default: val = this._relational(node[0], node[1], node[2]);
    }
    // Safe to memoize: kconfig rejects dependency loops, and choices are computed before their values
    this._memo.set(id, val);
    return val;
  }

  _leafType(id) {
    const node = this.exprs[id];
    return node[0] === "s" && this.syms[node[1]] ? this.syms[node[1]].type : "unknown";
  }

  /** E_LTH/E_LEQ/E_GTH/E_GEQ: numeric when both sides parse as int/hex, else string order. */
  _relational(op, a, b) {
    const s1 = this.leafStr(a);
    const s2 = this.leafStr(b);
    const t1 = this._leafType(a);
    const t2 = this._leafType(b);
    const parse = (text, otherType) => {
      if (otherType === "hex" || /^0[xX][0-9a-fA-F]+$/.test(text)) {
        return /^(0[xX])?[0-9a-fA-F]+$/.test(text) ? strtoll(text, 16) : null;
      }
      return /^-?[0-9]+$/.test(text) ? BigInt(text) : null;
    };
    let res;
    if (t1 === "string" && t2 === "string") {
      res = s1 > s2 ? 1 : (s1 < s2 ? -1 : 0);
    } else {
      const v1 = parse(s1, t2);
      const v2 = parse(s2, t1);
      if (v1 === null || v2 === null) return 0;
      res = v1 > v2 ? 1 : (v1 < v2 ? -1 : 0);
    }
    const ok = { "<": res < 0, "<=": res <= 0, ">": res > 0, ">=": res >= 0 }[op];
    return ok ? 2 : 0;
  }

  /** Kconfig source form of an expression; withValues appends "[=value]" like menuconfig's help. */
  exprStr(id, withValues = false) {
    if (id === null || id === undefined || id < 0) return "y";
    const fmt = (e, parentPrec) => {
      const node = this.exprs[e];
      switch (node[0]) {
        case "s": {
          const name = node[1];
          if (name.startsWith("<choice")) return "<choice>";
          if (withValues && !(name in TRI_OF)) return `${name} [=${this.isDefined(name) ? this.symStr(name) : "n"}]`;
          return name;
        }
        case "c": return `"${escapeConfigString(node[1])}"`;
        case "!": return `!${fmt(node[1], 3)}`;
        case "&":
        case "|": {
          const prec = node[0] === "&" ? 2 : 1;
          const text = `${fmt(node[1], prec)} ${node[0] === "&" ? "&&" : "||"} ${fmt(node[2], prec)}`;
          return prec < parentPrec ? `(${text})` : text;
        }
        default: return `${fmt(node[1], 4)}${node[0]}${fmt(node[2], 4)}`;
      }
    };
    return fmt(id, 0);
  }

  /** Top-level OR terms of an expression (reverse dependencies are an OR of `selector && cond`). */
  orTerms(id) {
    if (id === null || id === undefined || id < 0) return [];
    const node = this.exprs[id];
    if (node[0] === "|") return [...this.orTerms(node[1]), ...this.orTerms(node[2])];
    return [id];
  }

  /** Symbols referenced by an expression. */
  exprSymbols(id, out = new Set()) {
    if (id === null || id === undefined || id < 0) return out;
    const node = this.exprs[id];
    if (node[0] === "s") {
      if (!(node[1] in TRI_OF) && !node[1].startsWith("<choice")) out.add(node[1]);
    } else if (node[0] !== "c") {
      for (const child of node.slice(1)) this.exprSymbols(child, out);
    }
    return out;
  }

  // ------------------------------------------------------------------ symbol values
  isDefined(name) {
    const s = this.syms[name];
    return Boolean(s) && s.type !== "unknown";
  }

  symTri(name) {
    if (name in TRI_OF) return TRI_OF[name];
    if (!this.isDefined(name)) return 0;
    this._calc(name);
    const v = this._cur.get(name);
    return typeof v === "number" ? v : 0;
  }

  /** sym_get_string_value (constants and undefined symbols evaluate to their name). */
  symStr(name) {
    if (name in TRI_OF || !this.isDefined(name)) return name;
    this._calc(name);
    const v = this._cur.get(name);
    return typeof v === "number" ? TRI_CHAR[v] : v;
  }

  value(name) {
    return this.symStr(name);
  }

  modulesVal() {
    return this.symTri(this.modules);
  }

  /** sym_get_type: tristate degrades to bool without modules, or as a value of a y choice. */
  getType(name) {
    const s = this.syms[name];
    if (!s) return "unknown";
    if (s.type === "tristate") {
      if (s.choice && this.visibility(name) === 2) return "bool";
      if (name !== this.modules && this.modulesVal() === 0) return "bool";
    }
    return s.type;
  }

  hasUser(name) {
    return this.user.has(name);
  }

  /** sym_calc_visibility. */
  visibility(name) {
    const cached = this._vis.get(name);
    if (cached !== undefined) return cached;
    const s = this.syms[name];
    let vis = 0;
    const hideMod = this.sem.tristate_choice_mod_hidden && s.choice && s.type === "tristate" && this.symTri(s.choice) === 2;
    for (const pv of s.prompts) {
      let tri = this.exprTri(pv);
      if (hideMod && tri === 1) tri = 0; // m-visible tristate values of a y choice are hidden (newer kconfig)
      vis = Math.max(vis, tri);
    }
    if (vis === 1 && (s.type !== "tristate" || this.modulesVal() === 0)) vis = 2;
    this._vis.set(name, vis);
    if (s.choice) {
      this._dir.set(name, 2);
      this._rev.set(name, 0);
      this._implied.set(name, 0);
      return vis;
    }
    let dir = s.dir_dep >= 0 ? this.exprTri(s.dir_dep) : 2;
    if (dir === 1 && this.getType(name) === "bool") dir = 2;
    this._dir.set(name, dir);
    let rev = s.rev_dep >= 0 ? this.exprTri(s.rev_dep) : 0;
    if (rev === 1 && this.getType(name) === "bool") rev = 2;
    this._rev.set(name, rev);
    let implied = s.implied >= 0 ? this.exprTri(s.implied) : 0;
    if (implied === 1 && this.getType(name) === "bool") implied = 2;
    this._implied.set(name, implied);
    return vis;
  }

  revDep(name) {
    this.visibility(name);
    return this._rev.get(name);
  }

  dirDep(name) {
    this.visibility(name);
    return this._dir.get(name);
  }

  impliedVal(name) {
    this.visibility(name);
    return this._implied.get(name);
  }

  /** sym_get_default_prop: first default whose condition (incl. dependencies) is not n. */
  defaultProp(name) {
    for (const [valueId, visId] of this.syms[name].defaults) {
      const vis = this.exprTri(visId);
      if (vis !== 0) return { valueId, visId, vis };
    }
    return null;
  }

  _calc(name) {
    if (this._cur.has(name)) return;
    const s = this.syms[name];
    const stringType = STRING_TYPES.has(s.type);
    this._cur.set(name, stringType ? this._empty(s.type) : 0); // provisional value seen on recursion
    const modernWrite = Boolean(this.sem.write_on_nonzero_default);
    let write = false;
    const vis = this.visibility(name);
    if (modernWrite && vis !== 0) write = true;
    const rev = this._rev.get(name);
    const stype = this.getType(name);
    if (!stringType) {
      let newval;
      if (s.choice && vis === 2) {
        newval = this.choiceSelection(s.choice) === name ? 2 : 0;
      } else {
        newval = 0;
        let userDone = false;
        if (vis !== 0) {
          write = true;
          if (this.hasUser(name)) {
            newval = Math.min(TRI_OF[this.user.get(name)], vis);
            userDone = true;
          }
        }
        if (!userDone) {
          if (rev !== 0) write = true;
          if (!s.is_choice) {
            const prop = this.defaultProp(name);
            if (prop) {
              newval = Math.min(this.exprTri(prop.valueId), prop.vis);
              // Older kconfig wrote every symbol with an active default, newer only non-n ones
              if (newval !== 0 || !modernWrite) write = true;
            }
            const implied = this._implied.get(name) || 0;
            if (implied !== 0) {
              write = true;
              newval = Math.min(Math.max(newval, implied), this._dir.get(name));
            }
          }
        }
        newval = Math.max(newval, rev);
      }
      if (newval === 1 && stype === "bool") newval = 2;
      this._cur.set(name, newval);
    } else {
      let newval = this._empty(s.type);
      if (vis !== 0) write = true;
      if (vis !== 0 && this.hasUser(name)) {
        newval = this.user.get(name);
      } else {
        const prop = this.defaultProp(name);
        if (prop) {
          write = true;
          newval = this.leafStr(prop.valueId);
        }
      }
      this._cur.set(name, newval);
      if (s.type === "int" || s.type === "hex") this._validateRange(name);
    }
    if (s.is_choice && this._cur.get(name) === 2) this._sel.set(name, this._calcChoice(name));
    if (s.choice && this._choiceRawWrite(s.choice) && vis !== 0) write = true;
    if (s.is_choice) this._rawWrite.set(name, write);
    if (s.auto) write = false;
    this._write.set(name, write);
  }

  /** Value of an int/hex/string symbol without user value or default ("0"/"0x0" in newer kconfig). */
  _empty(type) {
    if (this.sem.int_hex_zero_default) return type === "int" ? "0" : (type === "hex" ? "0x0" : "");
    return "";
  }

  _choiceRawWrite(choice) {
    this._calc(choice);
    return this._rawWrite.get(choice) || false;
  }

  /** Active range [lo, hi] (BigInt) of an int/hex symbol, or null. */
  activeRange(name) {
    const s = this.syms[name];
    const base = s.type === "hex" ? 16 : 10;
    for (const [lo, hi, visId] of s.ranges) {
      if (this.exprTri(visId) === 0) continue;
      return { lo: this._rangeVal(lo, base), hi: this._rangeVal(hi, base), base };
    }
    return null;
  }

  _validateRange(name) {
    const s = this.syms[name];
    const base = s.type === "hex" ? 16 : 10;
    for (const [lo, hi, visId] of s.ranges) {
      if (this.exprTri(visId) === 0) continue;
      const val = strtoll(this._cur.get(name), base);
      let bound = this._rangeVal(lo, base);
      if (val >= bound) {
        bound = this._rangeVal(hi, base);
        if (val <= bound) return;
      }
      this._cur.set(name, s.type === "int" ? bound.toString() : `0x${(bound & MASK64).toString(16)}`);
      return;
    }
  }

  _rangeVal(id, base) {
    const node = this.exprs[id];
    if (node[0] === "s" && this.isDefined(node[1])) {
      const t = this.syms[node[1]].type;
      if (t === "int") base = 10;
      else if (t === "hex") base = 16;
    }
    return strtoll(this.leafStr(id), base);
  }

  /** sym_choice_default. */
  choiceDefault(choice) {
    const s = this.syms[choice];
    for (const [valueId, visId] of s.defaults) {
      if (this.exprTri(visId) === 0) continue;
      const node = this.exprs[valueId];
      if (node[0] === "s" && this.syms[node[1]] && this.visibility(node[1]) !== 0) return node[1];
    }
    for (const member of s.members) if (this.visibility(member) !== 0) return member;
    return null;
  }

  /** sym_calc_choice. */
  _calcChoice(choice) {
    for (const member of this.syms[choice].members) this.visibility(member);
    let selected = this.choiceUser.get(choice);
    if (selected && this.syms[selected] && this.visibility(selected) !== 0) return selected;
    selected = this.choiceDefault(choice);
    if (selected === null) this._cur.set(choice, 0);
    return selected;
  }

  choiceSelection(choice) {
    this._calc(choice);
    return this._cur.get(choice) === 2 ? (this._sel.get(choice) ?? null) : null;
  }

  writes(name) {
    this._calc(name);
    return this._write.get(name) || false;
  }

  /** sym_is_changable: the user can pick a value (visible above what selects force). */
  isChangeable(name) {
    return this.visibility(name) > this.revDep(name);
  }

  // ------------------------------------------------------------------ setting values (symbol.c)
  tristateWithinRange(name, val) {
    const s = this.syms[name];
    const vis = this.visibility(name);
    const stype = this.getType(name);
    if (vis === 0 || (stype !== "bool" && stype !== "tristate")) return false;
    if (stype === "bool" && val === 1) return false;
    if (vis <= this.revDep(name)) return false;
    if (s.choice && vis === 2) return val === 2;
    return val >= this.revDep(name) && val <= vis;
  }

  /** sym_set_tristate_value: refuses values kconfig would not allow. */
  setTristate(name, val) {
    const old = this.symTri(name);
    if (old !== val && !this.tristateWithinRange(name, val)) return false;
    const s = this.syms[name];
    if (s.choice && val === 2) {
      this.choiceUser.set(s.choice, name);
      if (!this.user.has(s.choice)) this.user.set(s.choice, "n");
      for (const member of this.syms[s.choice].members) {
        if (this.visibility(member) !== 0 && !this.user.has(member)) this.user.set(member, "n");
      }
    }
    this.user.set(name, TRI_CHAR[val]);
    this.invalidate();
    return true;
  }

  stringWithinRange(name, text) {
    const s = this.syms[name];
    if (s.type === "string") return true;
    if (s.type === "int" || s.type === "hex") {
      if (!stringValid(s.type, text)) return false;
      const range = this.activeRange(name);
      if (!range) return true;
      const val = strtoll(text, range.base);
      return val >= range.lo && val <= range.hi;
    }
    if ((s.type === "bool" || s.type === "tristate") && text) {
      const key = text[0].toLowerCase();
      return key in TRI_OF && this.tristateWithinRange(name, TRI_OF[key]);
    }
    return false;
  }

  /** sym_set_string_value (bool/tristate accept y/m/n). Returns false when kconfig would refuse. */
  setValue(name, text) {
    const s = this.syms[name];
    if (!s) return false;
    if (s.type === "bool" || s.type === "tristate") {
      const key = String(text || "").slice(0, 1).toLowerCase();
      return key in TRI_OF && this.setTristate(name, TRI_OF[key]);
    }
    let value = String(text ?? "");
    if (!this.stringWithinRange(name, value)) return false;
    if (s.type === "hex" && !/^0x/i.test(value)) value = `0x${value}`;
    this.user.set(name, value);
    this.invalidate();
    return true;
  }

  /** The value `toggle()` would set: the next of n -> m -> y -> n that is in range (the current one if none). */
  toggleTarget(name) {
    const old = this.symTri(name);
    for (let step = 1; step < 3; step++) {
      const next = (old + step) % 3;
      if (this.tristateWithinRange(name, next)) return next;
    }
    return old;
  }

  /** sym_toggle_tristate_value: n -> m -> y -> n, skipping values out of range. */
  toggle(name) {
    const next = this.toggleTarget(name);
    if (next !== this.symTri(name)) this.setTristate(name, next);
    return this.symTri(name);
  }

  /**
   * What other options lose if `name` is set to `text` (as `setValue()` would): bool/tristate options whose value
   * drops and other options no longer written to .config, each with the reason. Members of the same choice are
   * left out (selecting one deselects the others). The configuration is left as it was.
   * @returns {null | Array<{name: string, before: string, after: string, reason: string}>} null if kconfig refuses
   */
  previewLowered(name, text) {
    const target = this.syms[name];
    if (!target) return null;
    const snapshot = (n) => {
      const t = this.syms[n].type;
      if (t === "bool" || t === "tristate") return this.symTri(n);
      return this.writes(n) ? this.symStr(n) : null;
    };
    const names = Object.keys(this.syms).filter(
      (n) => n !== name && this.isDefined(n) && !this.syms[n].is_choice && !(target.choice && this.syms[n].choice === target.choice)
    );
    const before = new Map(names.map((n) => [n, snapshot(n)]));
    const saved = [new Map(this.user), new Map(this.choiceUser), new Map(this.choiceDefTri)];
    const lowered = [];
    try {
      if (!this.setValue(name, text)) return null;
      for (const n of names) {
        const was = before.get(n);
        const now = snapshot(n);
        const s = this.syms[n];
        const isTri = typeof was === "number";
        if (isTri ? !(now < was) : !(was !== null && now === null)) continue;
        let reason;
        if (s.dir_dep >= 0 && this.dirDep(n) < (isTri ? was : 2)) reason = `depends on ${this.exprStr(s.dir_dep, true)}`;
        else if (isTri && s.rev_dep >= 0 && this.revDep(n) < was) reason = `no longer selected: ${this.exprStr(s.rev_dep, true)}`;
        else if (s.prompts.length && this.visibility(n) === 0) reason = `prompt hidden: ${s.prompts.map((pv) => this.exprStr(pv, true)).join(" || ")}`;
        else reason = "its default no longer applies";
        lowered.push({
          name: n,
          before: isTri ? TRI_CHAR[was] : was,
          after: isTri ? TRI_CHAR[now] : "(not set)",
          reason,
        });
      }
    } finally {
      for (const [map, copy] of [[this.user, saved[0]], [this.choiceUser, saved[1]], [this.choiceDefTri, saved[2]]]) {
        map.clear();
        for (const [k, v] of copy) map.set(k, v);
      }
      this.invalidate();
    }
    return lowered.sort((a, b) => a.name.localeCompare(b.name));
  }

  /** Why a value cannot be chosen (empty when it can). */
  refusalReason(name, val) {
    const s = this.syms[name];
    if (!s) return "symbol is not defined for this architecture";
    const vis = this.visibility(name);
    if (vis === 0) {
      if (!s.prompts.length) return "no prompt: value comes from defaults/select";
      return `prompt not visible: ${s.prompts.map((p) => this.exprStr(p, true)).join(" || ")}`;
    }
    if (typeof val === "number") {
      if (this.getType(name) === "bool" && val === 1) return this.syms[name].type === "tristate" ? "modules are disabled (MODULES=n)" : "bool symbols cannot be m";
      if (val < this.revDep(name)) return `forced by select: ${this.exprStr(s.rev_dep, true)}`;
      if (val > vis) return `limited to ${TRI_CHAR[vis]} by dependencies`;
      if (s.choice && vis === 2 && val !== 2) return "a choice value is deselected by selecting another member";
    } else if (s.type === "int" || s.type === "hex") {
      if (!stringValid(s.type, val)) return `not a valid ${s.type} value`;
      const r = this.activeRange(name);
      if (r) return `out of range [${s.type === "hex" ? `0x${r.lo.toString(16)}` : r.lo}..${s.type === "hex" ? `0x${r.hi.toString(16)}` : r.hi}]`;
    }
    return "";
  }

  // ------------------------------------------------------------------ .config input (confdata.c)
  /** conf_read(): load a .config/defconfig as user values. Returns a report of ignored entries. */
  loadConfig(text) {
    this.user.clear();
    this.choiceUser.clear();
    this.choiceDefTri.clear();
    const report = { unknown: [], invalid: [], warnings: [] };
    const choiceFlag = new Map(this.choices.map((c) => [c, true]));
    const choiceTri = new Map(this.choices.map((c) => [c, 0]));

    const setSym = (name, val) => {
      this.user.set(name, val);
      const choice = this.syms[name].choice;
      if (!choice) return;
      const tri = TRI_OF[val] ?? 0;
      if (tri === 1 && choiceTri.get(choice) === 2) {
        report.warnings.push(`${name} creates inconsistent choice state`);
        choiceFlag.set(choice, false);
      } else if (tri === 2) {
        if (choiceTri.get(choice) !== 0) report.warnings.push(`override: ${name} changes choice state`);
        this.choiceUser.set(choice, name);
      }
      choiceTri.set(choice, Math.max(choiceTri.get(choice), tri));
    };

    for (const raw of String(text || "").split("\n")) {
      const line = raw.replace(/\r$/, "");
      if (line.startsWith("#")) {
        const m = /^# CONFIG_(\S+) is not set/.exec(line);
        if (!m) continue;
        const name = m[1];
        if (!this.syms[name]) {
          report.unknown.push(name);
          continue;
        }
        const t = this.syms[name].type;
        if (t === "bool" || t === "tristate") setSym(name, "n");
        continue;
      }
      if (!line.startsWith("CONFIG_")) {
        if (line.trim()) report.warnings.push(`unexpected data: ${line.slice(0, 80)}`);
        continue;
      }
      const eq = line.indexOf("=");
      if (eq < 0) continue;
      const name = line.slice(7, eq);
      const val = line.slice(eq + 1);
      const s = this.syms[name];
      if (!s || s.is_choice) {
        report.unknown.push(name);
        continue;
      }
      if (s.type === "bool" || s.type === "tristate") {
        if (s.type === "tristate" && val[0] === "m") setSym(name, "m");
        else if (val[0] === "y" || val[0] === "n") setSym(name, val[0]);
        else report.invalid.push({ symbol: name, value: val });
      } else if (s.type === "string") {
        if (!val.startsWith('"')) continue;
        let out = "";
        let closed = false;
        for (let i = 1; i < val.length; i++) {
          let ch = val[i];
          if (ch === '"') {
            closed = true;
            break;
          }
          if (ch === "\\" && i + 1 < val.length) ch = val[++i];
          out += ch;
        }
        if (!closed) report.invalid.push({ symbol: name, value: val });
        else this.user.set(name, out);
      } else if (s.type === "int" || s.type === "hex") {
        if (stringValid(s.type, val)) this.user.set(name, val);
        else report.invalid.push({ symbol: name, value: val });
      }
    }
    for (const choice of this.choices) {
      const tri = TRI_CHAR[choiceTri.get(choice)];
      this.user.set(choice, tri);
      this.choiceDefTri.set(choice, tri);
    }
    this.invalidate();

    // conf_read: count values that would not be saved as loaded, settle choice "has value" flags
    let unsaved = 0;
    for (const [name, s] of Object.entries(this.syms)) {
      if (s.type === "unknown") continue;
      if (s.is_choice) {
        for (const member of s.members) {
          if (this.visibility(member) !== 0 && !this.user.has(member)) choiceFlag.set(name, false);
        }
        continue;
      }
      if (s.auto) continue;
      const written = this.writes(name);
      if (this.hasUser(name) && written) {
        if (this.user.get(name) !== this.symStr(name)) unsaved++;
      } else if (this.hasUser(name) || written) {
        unsaved++;
      }
    }
    for (const [choice, keep] of choiceFlag) if (!keep) this.user.delete(choice);
    for (const name of [...this.user.keys()]) {
      const s = this.syms[name];
      if (!s || s.choice) continue;
      if (this.visibility(name) === 0 && !unsaved) {
        this.user.delete(name);
      } else if (STRING_TYPES.has(s.type) && !this.stringWithinRange(name, this.user.get(name))) {
        this.user.delete(name);
        unsaved++;
      }
    }
    this.invalidate();
    report.unsaved = unsaved;
    return report;
  }

  /** Load {NAME: value} assignments (bare or CONFIG_ keys) with conf_read semantics. */
  loadAssignments(assignments) {
    return this.loadConfig(this.assignmentsToConfig(assignments));
  }

  /** Render raw assignments as .config lines using the model's symbol types. */
  assignmentsToConfig(assignments) {
    const seen = new Set();
    const lines = [];
    for (const [key, raw] of Object.entries(assignments || {})) {
      const name = key.startsWith("CONFIG_") ? key.slice(7) : key;
      if (seen.has(name)) continue;
      seen.add(name);
      const val = raw === true ? "y" : raw === false ? "n" : String(raw ?? "");
      const type = this.syms?.[name]?.type;
      if (type === "string") lines.push(`CONFIG_${name}="${escapeConfigString(val)}"`);
      else if (val === "n") lines.push(`# CONFIG_${name} is not set`);
      else if (type === "int" || type === "hex" || /^(y|m|-?\d+|0[xX][0-9a-fA-F]+)$/.test(val)) lines.push(`CONFIG_${name}=${val}`);
      else lines.push(`CONFIG_${name}="${escapeConfigString(val)}"`);
    }
    return `${lines.join("\n")}\n`;
  }

  /** conf_set_all_new_symbols: "yes" / "mod" / "no" (all*config) or "default" (defconfig). */
  setAllNewSymbols(mode) {
    const fill = { yes: "y", mod: "m", no: "n" }[mode];
    for (const [name, s] of Object.entries(this.syms)) {
      if (this.hasUser(name) || s.type === "unknown") continue;
      const t = this.getType(name);
      if ((t === "bool" || t === "tristate") && fill !== undefined) this.user.set(name, fill);
    }
    this.invalidate();
    for (const choice of this.choices) {
      if (this.hasUser(choice)) continue;
      this._calc(choice);
      this.user.set(choice, this.choiceDefTri.get(choice) || "n");
      this.invalidate();
    }
  }

  // ------------------------------------------------------------------ menus and output
  /** menu_is_visible. */
  menuVisible(id) {
    const node = this.nodes[id];
    if (node.prompt === null || node.prompt === undefined) return false;
    if (node.visIf >= 0 && this.exprTri(node.visIf) === 0) return false;
    if (node.sym) this._calc(node.sym);
    if (this.exprTri(node.pvis) !== 0) return true;
    if (!node.sym || this.symTri(node.sym) === 0) return false;
    return node.children.some((c) => this.menuVisible(c));
  }

  symbolLine(name) {
    const s = this.syms[name];
    if (s.type === "bool" || s.type === "tristate") {
      const tri = this.symTri(name);
      return tri === 0 ? `# CONFIG_${name} is not set` : `CONFIG_${name}=${TRI_CHAR[tri]}`;
    }
    if (s.type === "string") return `CONFIG_${name}="${escapeConfigString(this.symStr(name))}"`;
    if (s.type === "int" || s.type === "hex") return `CONFIG_${name}=${this.symStr(name)}`;
    return null;
  }

  /** conf_write(): the full .config, in menu order, with menu comment blocks. */
  writeConfig() {
    const out = ["#", `# ${this.model.configHeader}`, `# ${this.model.mainmenu}`, "#"];
    const written = new Set();
    const endComments = Boolean(this.sem.menu_end_comments);
    let needNewline = false;
    const walk = (id) => {
      const node = this.nodes[id];
      const sym = node.sym;
      if (!sym) {
        if (this.menuVisible(id)) {
          out.push("", "#", `# ${node.prompt}`, "#");
          needNewline = false;
        }
      } else if (!this.syms[sym].is_choice && !written.has(sym) && this.writes(sym)) {
        const line = this.symbolLine(sym);
        if (line !== null) {
          if (needNewline) {
            out.push("");
            needNewline = false;
          }
          written.add(sym);
          out.push(line);
        }
      }
      for (const child of node.children) walk(child);
      if (endComments && !sym && node.kind === "menu" && this.menuVisible(id)) {
        out.push(`# end of ${node.prompt}`);
        needNewline = true;
      }
    };
    for (const child of this.nodes[0].children) walk(child);
    return `${out.join("\n")}\n`;
  }

  /** sym_get_string_default. */
  stringDefault(name) {
    const s = this.syms[name];
    let val = 0;
    let text = "";
    const prop = this.defaultProp(name);
    if (prop) {
      if (s.type === "bool" || s.type === "tristate") val = Math.min(this.exprTri(prop.valueId), prop.vis);
      else text = this.leafStr(prop.valueId);
    }
    val = Math.max(val, this.revDep(name));
    if (val === 1 && !s.choice && this.modulesVal() === 0) val = 2;
    if (s.type === "bool" && val === 1) val = 2;
    return s.type === "bool" || s.type === "tristate" ? TRI_CHAR[val] : text;
  }

  /** conf_write_defconfig(): minimal configuration (make savedefconfig). */
  writeDefconfig() {
    const out = [];
    const written = new Set();
    for (const node of this.nodes.slice(1)) {
      const sym = node.sym;
      if (!sym || this.syms[sym].is_choice || written.has(sym) || !this.writes(sym)) continue;
      written.add(sym);
      if (!this.isChangeable(sym)) continue;
      if (this.symStr(sym) === this.stringDefault(sym)) continue;
      const choice = this.syms[sym].choice;
      if (choice && !this.syms[choice].optional && sym === this.choiceDefault(choice)
        && this.syms[sym].type === "bool" && this.symTri(sym) === 2) continue;
      const line = this.symbolLine(sym);
      if (line !== null) out.push(line);
    }
    return out.length ? `${out.join("\n")}\n` : "";
  }

  /** Every written symbol's value: what .config / autoconf.h contain (bare names). */
  configValues() {
    const values = {};
    for (const [name, s] of Object.entries(this.syms)) {
      if (s.type === "unknown" || s.is_choice) continue;
      if (this.writes(name)) values[name] = this.symStr(name);
    }
    return values;
  }

  /** Symbols whose `select` names `name`. */
  selectors(name) {
    if (!this._selectors) {
      this._selectors = new Map();
      for (const [n, s] of Object.entries(this.syms)) {
        for (const [target] of s.selects) {
          if (!this._selectors.has(target)) this._selectors.set(target, []);
          this._selectors.get(target).push(n);
        }
      }
    }
    return this._selectors.get(name) || [];
  }

  /** Enclosing menu prompts of a menu node (menuconfig "Location:"). */
  menuPath(id) {
    const path = [];
    let parent = this.nodes[id]?.parent;
    while (parent > 0) {
      const node = this.nodes[parent];
      if (node.prompt) path.push(node.prompt);
      parent = node.parent;
    }
    return path.reverse();
  }
}
