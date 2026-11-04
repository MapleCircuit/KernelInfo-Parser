/**
 * makefile.js - Makefile & Kbuild syntax grammar for the Linux kernel build system.
 */

export const MAKEFILE_KEYWORDS = new Set([
  "ifeq", "ifneq", "ifdef", "ifndef", "else", "endif", "include", "-include",
  "sinclude", "export", "unexport", "override", "define", "endef", "vpath"
]);

export const MAKEFILE_FUNCTIONS = new Set([
  "subst", "patsubst", "strip", "findstring", "filter", "filter-out",
  "sort", "word", "words", "firstword", "lastword", "wordlist", "wildcard",
  "dir", "notdir", "suffix", "basename", "addsuffix", "addprefix", "join",
  "shell", "error", "warning", "info", "origin", "flavor", "foreach",
  "if", "or", "and", "call", "eval", "value"
]);

export const makefileGrammar = {
  id: "makefile",
  name: "Makefile",
  ftypes: [],
  extensions: [".mk"],
  filenames: ["Makefile", "Kbuild"],
  shebangs: [],
  keywords: MAKEFILE_KEYWORDS,
  types: MAKEFILE_FUNCTIONS,
  hasPreprocessor: false,
  supportsInclude: false,
  isCommentLine(trimmed) {
    return trimmed.startsWith("#");
  },
  isType(val) {
    return MAKEFILE_FUNCTIONS.has(val);
  }
};

