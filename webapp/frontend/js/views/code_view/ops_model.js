/**
 * ops_model.js - DOM-free helper of the ops implementations dialog (N1).
 */
/** The struct of the `struct S name = {` initializer enclosing `line` (1-based), from the tab's text. */
export function enclosingStruct(lines, line) {
  let depth = 0;
  for (let i = Math.min(lines.length, line) - 1; i >= 0 && i >= line - 400; i--) {
    const l = lines[i].replace(/\/\*.*?\*\/|\/\/.*$|"(?:[^"\\]|\\.)*"/g, "");
    const m = /\bstruct\s+([A-Za-z_]\w*)\s+[A-Za-z_]\w*\s*(\[[^\]]*\])?\s*(?:__\w+\s*)*=\s*\{?/.exec(l);
    if (m && depth <= 0) return m[1];
    for (const c of [...l].reverse()) {
      if (c === "}") depth++;
      else if (c === "{") depth--;
    }
    if (depth < -2) break;
  }
  return "";
}

/** What a token is on its line: "field" (`.name =`), "function" (`= name` / `= &name`) or "" (unknown). */
export function tokenRole(lineText, name) {
  const esc = name.replace(/[^\w]/g, "");
  if (new RegExp(`\\.\\s*${esc}\\s*=`).test(lineText || "")) return "field";
  if (new RegExp(`=\\s*&?\\s*${esc}\\b(?!\\s*\\()`).test(lineText || "")) return "function";
  return "";
}
