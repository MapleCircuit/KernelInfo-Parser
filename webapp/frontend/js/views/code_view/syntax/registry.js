/**
 * registry.js - Language registry and syntax detector for KernelInfo-Parser.
 * Detects grammar by ftype, file extension, exact filename, or first-line shebang.
 */

import { cGrammar } from "./languages/c.js";
import { rustGrammar } from "./languages/rust.js";
import { pythonGrammar } from "./languages/python.js";
import { shellGrammar } from "./languages/shell.js";
import { perlGrammar } from "./languages/perl.js";
import { assemblyGrammar } from "./languages/assembly.js";
import { makefileGrammar } from "./languages/makefile.js";
import { kconfigGrammar } from "./languages/kconfig.js";
import { dtsGrammar } from "./languages/dts.js";
import { yamlGrammar } from "./languages/yaml.js";

export const ALL_GRAMMARS = [
  cGrammar,
  rustGrammar,
  pythonGrammar,
  shellGrammar,
  perlGrammar,
  assemblyGrammar,
  makefileGrammar,
  kconfigGrammar,
  dtsGrammar,
  yamlGrammar
];

export const DEFAULT_FALLBACK_GRAMMAR = {
  id: "raw",
  name: "Raw",
  ftypes: [7],
  extensions: [],
  filenames: [],
  shebangs: [],
  keywords: new Set(),
  types: new Set(),
  hasPreprocessor: false,
  supportsInclude: false,
  isCommentLine(trimmed) {
    return trimmed.startsWith("#") || trimmed.startsWith("//") || trimmed.startsWith("/*");
  },
  isType() {
    return false;
  }
};

/**
 * Detect language grammar based on ftype, filePath, and optional first line content (shebang).
 *
 * @param {number|null} ftype - Database ftype (1: C, 2: Kconfig, 3: Rust, 4: ASM, 7: Raw)
 * @param {string} filePath - Path or filename of the current file
 * @param {string} [firstLine=""] - First line of content for shebang inspection
 * @returns {object} The matched language grammar definition
 */
export function detectGrammar(ftype, filePath = "", firstLine = "") {
  const normPath = (filePath || "").trim();
  const basename = normPath ? normPath.split("/").pop() : "";

  // 1. Explicit ftype matches from database/parser
  if (ftype === 1) return cGrammar;
  if (ftype === 2) return kconfigGrammar;
  if (ftype === 3) return rustGrammar;
  if (ftype === 4) return assemblyGrammar;

  // 2. Exact filename or filename pattern matching
  if (basename) {
    if (basename === "Makefile" || basename === "Kbuild" || basename.startsWith("Makefile.")) {
      return makefileGrammar;
    }
    if (basename === "Kconfig" || basename.startsWith("Kconfig.")) {
      return kconfigGrammar;
    }
  }

  // 3. File extension matching (case-insensitive)
  if (basename) {
    const dotIdx = basename.lastIndexOf(".");
    if (dotIdx !== -1) {
      const ext = basename.slice(dotIdx).toLowerCase();
      // Handle assembly .S (case-sensitive convention for preprocessed assembly)
      const exactExt = basename.slice(dotIdx);
      if (exactExt === ".S" || exactExt === ".s" || ext === ".asm") {
        return assemblyGrammar;
      }
      if (ext === ".c" || ext === ".h" || ext === ".i") {
        return cGrammar;
      }
      if (ext === ".rs") {
        return rustGrammar;
      }
      if (ext === ".py") {
        return pythonGrammar;
      }
      if (ext === ".sh" || ext === ".bash" || ext === ".zsh") {
        return shellGrammar;
      }
      if (ext === ".pl" || ext === ".pm") {
        return perlGrammar;
      }
      if (ext === ".mk") {
        return makefileGrammar;
      }
      if (ext === ".dts" || ext === ".dtsi") {
        return dtsGrammar;
      }
      if (ext === ".yaml" || ext === ".yml") {
        return yamlGrammar;
      }
    }
  }

  // 4. First-line shebang inspection for extensionless scripts
  if (firstLine && firstLine.startsWith("#!")) {
    for (const g of [shellGrammar, pythonGrammar, perlGrammar]) {
      if (g.shebangs.some((regex) => regex.test(firstLine))) {
        return g;
      }
    }
  }

  return DEFAULT_FALLBACK_GRAMMAR;
}

