/**
 * shell.js - Shell script syntax grammar for the Linux kernel build & verification scripts.
 */

export const SHELL_KEYWORDS = new Set([
  "if", "then", "else", "elif", "fi", "case", "esac", "for", "while",
  "until", "do", "done", "in", "function", "select", "time", "return",
  "exit", "export", "local", "readonly", "declare", "typeset", "unset",
  "shift", "source", "alias"
]);

export const SHELL_BUILTINS = new Set([
  "echo", "printf", "read", "cd", "pwd", "test", "eval", "exec", "trap",
  "set", "shopt", "true", "false"
]);

export const shellGrammar = {
  id: "shell",
  name: "Shell",
  ftypes: [],
  extensions: [".sh", ".bash", ".zsh"],
  filenames: [],
  shebangs: [/^#!.*\b(sh|bash|zsh)\b/],
  keywords: SHELL_KEYWORDS,
  types: SHELL_BUILTINS,
  hasPreprocessor: false,
  supportsInclude: false,
  isCommentLine(trimmed) {
    return trimmed.startsWith("#");
  },
  isType(val) {
    return SHELL_BUILTINS.has(val);
  }
};

