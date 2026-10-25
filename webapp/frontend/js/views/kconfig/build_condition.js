/**
 * build_condition.js - Evaluate Kbuild build conditions (GET /api/kconfig/{v}/build) against a configuration.
 *
 * Expressions come from parser/kbuild_conditions.py (whose `evaluate()` is the reference twin):
 * "y" / "m" / "n" / "?" constants, {sym}, {def}, {eq: [a, b]} (make strings of literals and {val}),
 * {not}, {and}, {or}. Results are "y" (built-in), "m" (module), "n" (not compiled) or "?" (unknown).
 */

/**
 * Value lookup for a definition context (`state.definitionContext()`): raw value of a bare symbol,
 * undefined when unknown. With a strict (resolved) configuration, absent symbols are "n".
 */
export function configLookup({ assignments = {}, strict = false } = {}) {
  return (sym) => {
    const v = assignments[sym] ?? assignments[`CONFIG_${sym}`];
    if (v === undefined || v === null) return strict ? "n" : undefined;
    return String(v);
  };
}

function rawValue(valueOf, sym) {
  const v = valueOf(sym);
  if (v === undefined) return undefined;
  return v === "n" ? "" : v;
}

export function evalBuild(expr, valueOf) {
  if (typeof expr === "string") return expr;
  if (!expr || typeof expr !== "object") return "?";
  if ("sym" in expr) {
    const v = valueOf(expr.sym);
    if (v === undefined) return "?";
    return v === "y" || v === "m" ? v : "n";
  }
  if ("def" in expr) {
    const v = rawValue(valueOf, expr.def);
    return v === undefined ? "?" : (v ? "y" : "n");
  }
  if ("eq" in expr) {
    const sides = [];
    for (const side of expr.eq) {
      let text = "";
      for (const part of side) {
        const v = typeof part === "object" ? rawValue(valueOf, part.val) : part;
        if (v === undefined) return "?";
        text += v;
      }
      sides.push(text.trim());
    }
    return sides[0] === sides[1] ? "y" : "n";
  }
  if ("not" in expr) {
    const v = evalBuild(expr.not, valueOf);
    return v === "?" ? "?" : (v === "n" ? "y" : "n");
  }
  if ("and" in expr) {
    const vals = expr.and.map((e) => evalBuild(e, valueOf));
    if (vals.includes("n")) return "n";
    if (vals.includes("?")) return "?";
    return vals.includes("m") ? "m" : "y";
  }
  if ("or" in expr) {
    const vals = expr.or.map((e) => evalBuild(e, valueOf));
    for (const v of ["y", "m", "?"]) if (vals.includes(v)) return v;
    return "n";
  }
  return "?";
}

/** CONFIG_* symbols an expression reads. */
function symbolsOf(expr, out = new Set()) {
  if (!expr || typeof expr !== "object") return out;
  if ("sym" in expr) out.add(expr.sym);
  else if ("def" in expr) out.add(expr.def);
  else if ("eq" in expr) expr.eq.flat().forEach((p) => typeof p === "object" && out.add(p.val));
  else if ("not" in expr) symbolsOf(expr.not, out);
  else (expr.and || expr.or || []).forEach((e) => symbolsOf(e, out));
  return out;
}

/**
 * Why an expression is "n": the symbols of its failing terms with their current values, e.g.
 * ["CONFIG_E1000=n"] (for an `or`, the first alternative's; `more` counts the other alternatives).
 */
export function explainBuild(expr, valueOf) {
  const failing = (e) => {
    if (evalBuild(e, valueOf) !== "n" || typeof e !== "object") return [];
    if ("and" in e) return e.and.flatMap(failing);
    if ("or" in e) return failing(e.or[0]);
    return [...symbolsOf(e)];
  };
  const syms = [...new Set(failing(expr))];
  return {
    symbols: syms.map((s) => `CONFIG_${s}=${valueOf(s) ?? "n"}`),
    more: expr && typeof expr === "object" && "or" in expr ? expr.or.length - 1 : 0,
  };
}
