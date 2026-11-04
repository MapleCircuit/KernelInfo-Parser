/**
 * perl.js - Perl syntax grammar for Linux kernel tools (e.g. checkpatch.pl, get_maintainer.pl).
 */

export const PERL_KEYWORDS = new Set([
  "sub", "my", "our", "local", "use", "package", "require", "strict",
  "warnings", "if", "unless", "else", "elsif", "while", "until", "for",
  "foreach", "do", "next", "last", "redo", "return", "die", "warn",
  "eval", "eq", "ne", "lt", "gt", "le", "ge", "cmp", "and", "or", "not"
]);

export const perlGrammar = {
  id: "perl",
  name: "Perl",
  ftypes: [],
  extensions: [".pl", ".pm"],
  filenames: [],
  shebangs: [/^#!.*\bperl\b/],
  keywords: PERL_KEYWORDS,
  types: new Set(["STDIN", "STDOUT", "STDERR"]),
  hasPreprocessor: false,
  supportsInclude: false,
  isCommentLine(trimmed) {
    return trimmed.startsWith("#");
  },
  isType(val) {
    return val === "STDIN" || val === "STDOUT" || val === "STDERR";
  }
};

