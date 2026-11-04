/**
 * python.js - Python syntax grammar for the Linux kernel tools/scripts.
 */

export const PYTHON_KEYWORDS = new Set([
  "def", "class", "import", "from", "as", "return", "yield", "if", "elif",
  "else", "for", "while", "break", "continue", "try", "except", "finally",
  "raise", "with", "pass", "lambda", "assert", "global", "nonlocal",
  "async", "await", "del", "in", "is", "not", "and", "or"
]);

export const PYTHON_TYPES = new Set([
  "int", "str", "float", "bool", "bytes", "list", "dict", "set", "tuple",
  "len", "range", "print", "enumerate", "zip", "isinstance", "issubclass",
  "open", "super", "type", "object", "Exception", "ValueError", "TypeError",
  "True", "False", "None", "self", "cls"
]);

export const pythonGrammar = {
  id: "python",
  name: "Python",
  ftypes: [],
  extensions: [".py"],
  filenames: [],
  shebangs: [/^#!.*\bpython[0-9.]*\b/],
  keywords: PYTHON_KEYWORDS,
  types: PYTHON_TYPES,
  hasPreprocessor: false,
  supportsInclude: false,
  isCommentLine(trimmed) {
    return trimmed.startsWith("#");
  },
  isType(val) {
    if (PYTHON_TYPES.has(val)) return true;
    return /^[A-Z][a-zA-Z0-9_]*$/.test(val);
  }
};

