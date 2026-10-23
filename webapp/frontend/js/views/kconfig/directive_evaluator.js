/**
 * directive_evaluator.js - C Preprocessor #if/#ifdef Conditional Dimming Evaluator.
 *
 * Computes which source lines are compiled out under the current Kconfig assignments.
 * Evaluation is three-valued: only CONFIG_* macros are known (from the resolved configuration,
 * following include/generated/autoconf.h semantics: CONFIG_FOO is defined for "y" and string/int
 * values, CONFIG_FOO_MODULE for "m"); any other macro is unknown, and a branch whose condition
 * is unknown is never dimmed — nor are its #else/#elif alternatives.
 */

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

export class DirectiveEvaluator {
  constructor(assignments = {}) {
    this.assignments = assignments;
  }

  setAssignments(assignments) {
    this.assignments = assignments || {};
  }

  /** Raw Kconfig value for CONFIG_<name> ("y", "m", "n", string/int) or undefined when unset. */
  valueOf(bare) {
    return this.assignments[`CONFIG_${bare}`] ?? this.assignments[bare];
  }

  /** defined(MACRO) for a CONFIG_* macro; UNKNOWN for any other macro. */
  isDefined(macro) {
    if (!macro.startsWith("CONFIG_")) return UNKNOWN;
    let bare = macro.slice(7);
    if (bare.endsWith("_MODULE")) {
      const base = this.valueOf(bare.slice(0, -7));
      if (base !== undefined) return base === "m" ? TRUE : FALSE;
    }
    const val = this.valueOf(bare);
    if (val === undefined || val === "n" || val === "") return FALSE;
    return val === "m" ? FALSE : TRUE;
  }

  /** IS_ENABLED / IS_BUILTIN / IS_MODULE / IS_REACHABLE on a CONFIG_* macro. */
  configTest(fn, macro) {
    if (!macro.startsWith("CONFIG_")) return UNKNOWN;
    const val = this.valueOf(macro.slice(7));
    const isY = val !== undefined && val !== "n" && val !== "m" && val !== "";
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
        if (val === undefined || val === "n") return 0;
        if (val === "y") return 1;
        const n = Number(val);
        return Number.isFinite(n) ? n : null;
      }
      return null;
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
