/**
 * directive_evaluator.js - C Preprocessor #if/#ifdef Conditional Dimming Evaluator.
 *
 * Computes which source lines are compiled out under the current Kconfig assignments.
 * Evaluation is three-valued: only CONFIG_* macros are known (from the resolved configuration,
 * following include/generated/autoconf.h semantics: CONFIG_FOO is defined for "y" and every
 * string/int/hex value (even an empty string), CONFIG_FOO_MODULE for "m"; symbols that are "n" or
 * absent from the configuration are undefined); any other macro is unknown, and a branch whose condition
 * is unknown is never dimmed — nor are its #else/#elif alternatives.
 *
 * Compiler-predefined macros of the configured ARCH are known too (`archMacros()`): on x86_64
 * `__x86_64__` is defined and `__i386__` is not, `__KERNEL__` is defined, other architectures' macros
 * (`__arm__`, `__powerpc__`...) are undefined. With `partial: true` (an incomplete configuration such as
 * the x86_64 baseline used while the engine is off) symbols absent from the assignments are unknown
 * instead of "n".
 */

// Source types whose #if/#ifdef blocks follow C preprocessor semantics
export const PREPROCESSED_RE = /\.(c|h|S|lds|dts|dtsi|inc)$/i;

const TRUE = 1;
const FALSE = 0;
const UNKNOWN = -1;

function not(v) {
  return v === UNKNOWN ? UNKNOWN : (v ? FALSE : TRUE);
}

function and(a, b) {
  if (a === FALSE || b === FALSE) return FALSE;
  if (a === UNKNOWN || b === UNKNOWN) return UNKNOWN;
  return TRUE;
}

function or(a, b) {
  if (a === TRUE || b === TRUE) return TRUE;
  if (a === UNKNOWN || b === UNKNOWN) return UNKNOWN;
  return FALSE;
}

// ARCH value -> source directory (top Makefile SRCARCH aliases) and word size
const ARCH_ALIASES = {
  i386: ["x86", 32], x86_64: ["x86", 64], sparc32: ["sparc", 32], sparc64: ["sparc", 64], sh64: ["sh", 64],
  parisc64: ["parisc", 64], arm64: ["arm64", 64], ppc64: ["powerpc", 64], ia64: ["ia64", 64], alpha: ["alpha", 64],
  s390: ["s390", null], arm: ["arm", 32], m68k: ["m68k", 32],
};
// Compiler-predefined macros per source architecture: [always, 64-bit only, 32-bit only]
const ARCH_PREDEFINED = {
  x86: [[], ["__x86_64__", "__x86_64", "__amd64__", "__amd64"], ["__i386__", "__i386", "i386"]],
  arm: [["__arm__"], [], []],
  arm64: [["__aarch64__"], [], []],
  powerpc: [["__powerpc__", "__PPC__"], ["__powerpc64__", "__PPC64__"], []],
  sparc: [["__sparc__", "__sparc"], ["__sparc_v9__", "__arch64__"], []],
  mips: [["__mips__", "__mips"], ["__mips64"], []],
  s390: [["__s390__"], ["__s390x__"], []],
  ia64: [["__ia64__"], [], []],
  alpha: [["__alpha__"], [], []],
  m68k: [["__mc68000__", "__m68k__"], [], []],
  sh: [["__sh__"], [], []],
  parisc: [["__hppa__"], [], []],
  riscv: [["__riscv"], [], []],
  loongarch: [["__loongarch__"], [], []],
  microblaze: [["__microblaze__"], [], []],
  xtensa: [["__xtensa__"], [], []],
  arc: [["__arc__"], [], []],
  openrisc: [["__or1k__"], [], []],
  csky: [["__csky__"], [], []],
  hexagon: [["__hexagon__"], [], []],
  nios2: [["__nios2__"], [], []],
  blackfin: [["__bfin__"], [], []],
  avr32: [["__avr32__"], [], []],
  frv: [["__frv__"], [], []],
  cris: [["__CRIS__"], [], []],
  m32r: [["__m32r__"], [], []],
  h8300: [["__H8300__"], [], []],
  mn10300: [["__mn10300__"], [], []],
  score: [["__score__"], [], []],
  tile: [["__tile__"], [], []],
  unicore32: [["__unicore32__"], [], []],
};

/**
 * Known compiler macros for an ARCH value: { defined: Set, undefined: Set }. The word size comes from the
 * configuration (CONFIG_64BIT) when present, else from the ARCH value (x86_64, i386...).
 */
export function archMacros(arch, assignments = {}) {
  const id = String(arch || "").toLowerCase();
  const [srcarch, archBits] = ARCH_ALIASES[id] || [id, null];
  const cfg64 = assignments["64BIT"] ?? assignments.CONFIG_64BIT;
  const bits = cfg64 === "y" ? 64 : cfg64 === "n" ? 32 : archBits;
  const defined = new Set(["__KERNEL__"]);
  const undefinedSet = new Set(["__CHECKER__", "__GENKSYMS__"]);
  for (const [name, [always, only64, only32]] of Object.entries(ARCH_PREDEFINED)) {
    const own = name === srcarch || (srcarch === "um" && name === "x86");
    for (const m of always) (own ? defined : undefinedSet).add(m);
    // Word-size macros of the own architecture stay unknown when the word size is
    for (const [macros, size] of [[only64, 64], [only32, 32]]) {
      if (own && bits === null) continue;
      for (const m of macros) (own && bits === size ? defined : undefinedSet).add(m);
    }
  }
  if (bits === 64) defined.add("__LP64__").add("_LP64");
  else if (bits === 32) undefinedSet.add("__LP64__").add("_LP64");
  return { defined, undefined: undefinedSet };
}

export class DirectiveEvaluator {
  /**
   * @param {object} assignments  Kconfig values (bare and/or CONFIG_ keys).
   * @param {{partial?: boolean, macros?: {defined: Set, undefined: Set}}} options
   */
  constructor(assignments = {}, { partial = false, macros = null } = {}) {
    this.assignments = assignments;
    this.partial = partial;
    this.macros = macros || { defined: new Set(), undefined: new Set() };
  }

  setAssignments(assignments) {
    this.assignments = assignments || {};
  }

  setMacros(macros) {
    this.macros = macros || { defined: new Set(), undefined: new Set() };
  }

  /** defined(MACRO) of a compiler-predefined (non-CONFIG) macro: TRUE, FALSE or UNKNOWN. */
  predefined(macro) {
    if (this.macros.defined.has(macro)) return TRUE;
    if (this.macros.undefined.has(macro)) return FALSE;
    return UNKNOWN;
  }

  /** Raw Kconfig value for CONFIG_<name> ("y", "m", "n", string/int) or undefined when unset. */
  valueOf(bare) {
    return this.assignments[`CONFIG_${bare}`] ?? this.assignments[bare];
  }

  /** defined(MACRO) for a CONFIG_* or known predefined macro; UNKNOWN for any other macro. */
  isDefined(macro) {
    if (!macro.startsWith("CONFIG_")) return this.predefined(macro);
    let bare = macro.slice(7);
    if (bare.endsWith("_MODULE")) {
      const base = this.valueOf(bare.slice(0, -7));
      if (base !== undefined) return base === "m" ? TRUE : FALSE;
    }
    const val = this.valueOf(bare);
    if (val === undefined && this.partial) return UNKNOWN;
    if (val === undefined || val === null || val === "n") return FALSE;
    return val === "m" ? FALSE : TRUE;
  }

  /** IS_ENABLED / IS_BUILTIN / IS_MODULE / IS_REACHABLE on a CONFIG_* macro. */
  configTest(fn, macro) {
    if (!macro.startsWith("CONFIG_")) return UNKNOWN;
    const val = this.valueOf(macro.slice(7));
    if (val === undefined && this.partial) return UNKNOWN;
    // __is_defined() only sees macros expanding to 1: y, or an int symbol whose value is 1
    const isY = val === "y" || val === "1";
    const isM = val === "m";
    switch (fn) {
      case "IS_BUILTIN": return isY ? TRUE : FALSE;
      case "IS_MODULE": return isM ? TRUE : FALSE;
      default: return isY || isM ? TRUE : FALSE; // IS_ENABLED, IS_REACHABLE (module reachability unknown → enabled)
    }
  }

  /** Evaluate a #if / #elif expression to TRUE, FALSE or UNKNOWN. */
  evalExpression(expr) {
    const tokens = String(expr)
      .replace(/\/\*.*?\*\//g, " ")
      .replace(/\/\/.*$/, "")
      .match(/\d+[uUlL]*|[A-Za-z_]\w*|&&|\|\||==|!=|<=|>=|[()!<>]|\S/g) || [];
    let pos = 0;
    const peek = () => tokens[pos];
    const next = () => tokens[pos++];

    // Numeric values are only known for literals and int/hex CONFIG values
    const numeric = (tok) => {
      if (/^\d/.test(tok)) return parseInt(tok, /^0x/i.test(tok) ? 16 : 10);
      if (tok.startsWith("CONFIG_")) {
        const val = this.valueOf(tok.slice(7));
        if (val === undefined && this.partial) return null;
        if (val === undefined || val === "n") return 0;
        if (val === "y") return 1;
        const n = Number(val);
        return Number.isFinite(n) ? n : null;
      }
      // Known predefined macros expand to 1; known-undefined identifiers are 0 in #if
      const known = this.predefined(tok);
      return known === UNKNOWN ? null : known;
    };

    const primary = () => {
      const tok = next();
      if (tok === undefined) return { v: UNKNOWN };
      if (tok === "!") {
        const r = primary();
        return { v: not(r.v) };
      }
      if (tok === "(") {
        const r = orExpr();
        if (peek() === ")") next();
        return r;
      }
      if (tok === "defined") {
        let name;
        if (peek() === "(") {
          next();
          name = next();
          if (peek() === ")") next();
        } else {
          name = next();
        }
        return { v: name ? this.isDefined(name) : UNKNOWN };
      }
      if (/^(IS_ENABLED|IS_BUILTIN|IS_MODULE|IS_REACHABLE)$/.test(tok) && peek() === "(") {
        next();
        const name = next();
        if (peek() === ")") next();
        return { v: name ? this.configTest(tok, name) : UNKNOWN };
      }
      if (/^[A-Za-z_]\w*$/.test(tok) && peek() === "(") {
        // Unknown function-like macro: skip its arguments
        let depth = 0;
        do {
          const t = next();
          if (t === "(") depth++;
          else if (t === ")") depth--;
        } while (depth > 0 && pos < tokens.length);
        return { v: UNKNOWN };
      }
      const n = numeric(tok);
      if (n === null) return { v: UNKNOWN };
      return { v: n ? TRUE : FALSE, n };
    };

    const compare = () => {
      let left = primary();
      while (["==", "!=", "<", ">", "<=", ">="].includes(peek())) {
        const op = next();
        const right = primary();
        if (left.n === undefined || right.n === undefined) {
          left = { v: UNKNOWN };
          continue;
        }
        const a = left.n;
        const b = right.n;
        const res = op === "==" ? a === b : op === "!=" ? a !== b : op === "<" ? a < b : op === ">" ? a > b : op === "<=" ? a <= b : a >= b;
        left = { v: res ? TRUE : FALSE, n: res ? 1 : 0 };
      }
      return left;
    };

    const andExpr = () => {
      let left = compare();
      while (peek() === "&&") {
        next();
        left = { v: and(left.v, compare().v) };
      }
      return left;
    };

    function orExpr() {
      let left = andExpr();
      while (peek() === "||") {
        next();
        left = { v: or(left.v, andExpr().v) };
      }
      return left;
    }

    const result = orExpr();
    // Trailing tokens we could not parse (ternaries, arithmetic) make the result unreliable
    return pos < tokens.length ? UNKNOWN : result.v;
  }

  /**
   * Scan raw code lines and return a Set of 1-indexed line numbers that are compiled out.
   * Directive lines of a dead branch (#else/#elif/#endif) themselves stay undimmed.
   */
  evaluateDimmedLines(rawLines) {
    const dimmed = new Set();
    // Each frame: { active: tri-state of the current branch, taken: tri-state "an earlier branch was true", parentLive }
    const stack = [];
    const live = () => stack.every((f) => f.active !== FALSE);

    for (let idx = 0; idx < rawLines.length; idx++) {
      const text = rawLines[idx] || "";
      const lineNo = idx + 1;
      const m = /^\s*#\s*(\w+)\s*(.*)$/.exec(text);
      if (m) {
        let rest = m[2];
        // Directives may continue with trailing backslashes
        let j = idx;
        while (/\\\s*$/.test(rawLines[j] || "") && j + 1 < rawLines.length) {
          j++;
          rest = rest.replace(/\\\s*$/, " ") + (rawLines[j] || "");
        }
        const directive = m[1];
        // Whether the region enclosing this directive (excluding its own #if frame) is compiled
        let enclosingLive = null;
        if (directive === "ifdef" || directive === "ifndef") {
          enclosingLive = live();
          const name = (rest.match(/^\s*(\w+)/) || [])[1] || "";
          let v = this.isDefined(name);
          if (directive === "ifndef") v = not(v);
          stack.push({ active: v, taken: v, parentLive: enclosingLive });
        } else if (directive === "if") {
          enclosingLive = live();
          const v = this.evalExpression(rest);
          stack.push({ active: v, taken: v, parentLive: enclosingLive });
        } else if (directive === "elif" && stack.length) {
          const top = stack[stack.length - 1];
          enclosingLive = top.parentLive;
          const cond = this.evalExpression(rest);
          top.active = and(not(top.taken), cond);
          top.taken = or(top.taken, cond);
        } else if (directive === "else" && stack.length) {
          const top = stack[stack.length - 1];
          enclosingLive = top.parentLive;
          top.active = not(top.taken);
          top.taken = TRUE;
        } else if (directive === "endif" && stack.length) {
          enclosingLive = stack.pop().parentLive;
        }
        if (enclosingLive !== null) {
          if (!enclosingLive) for (let k = idx; k <= j; k++) dimmed.add(k + 1);
          idx = j;
          continue;
        }
      }
      if (!live()) dimmed.add(lineNo);
    }
    return dimmed;
  }
}
