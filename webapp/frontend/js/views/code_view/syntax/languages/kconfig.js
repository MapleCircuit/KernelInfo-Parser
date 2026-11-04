/**
 * kconfig.js - Kconfig syntax grammar for Linux kernel configuration files.
 */

export const KCONFIG_KEYWORDS = new Set([
  "config", "menuconfig", "menu", "endmenu", "choice", "endchoice",
  "if", "endif", "source", "mainmenu", "comment", "depends", "on",
  "select", "imply", "default", "range", "prompt", "help", "---help---",
  "visible", "option", "def_bool", "def_tristate"
]);

export const KCONFIG_TYPES = new Set([
  "bool", "tristate", "string", "hex", "int", "boolean"
]);

export const kconfigGrammar = {
  id: "kconfig",
  name: "Kconfig",
  ftypes: [2],
  extensions: [],
  filenames: ["Kconfig"],
  shebangs: [],
  keywords: KCONFIG_KEYWORDS,
  types: KCONFIG_TYPES,
  hasPreprocessor: false,
  supportsInclude: false,
  isCommentLine(trimmed) {
    return trimmed.startsWith("#");
  },
  isType(val) {
    return KCONFIG_TYPES.has(val);
  }
};

