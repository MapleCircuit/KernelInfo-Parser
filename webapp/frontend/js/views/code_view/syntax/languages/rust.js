/**
 * rust.js - Rust language syntax grammar for the Linux kernel.
 */

export const RUST_KEYWORDS = new Set([
  "as", "async", "await", "break", "const", "continue", "crate", "dyn",
  "else", "enum", "extern", "false", "fn", "for", "if", "impl", "in",
  "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return",
  "self", "Self", "static", "struct", "super", "trait", "true", "type",
  "union", "unsafe", "use", "where", "while", "yield", "try", "macro_rules",
  "default"
]);

export const RUST_TYPES = new Set([
  "bool", "char", "str", "u8", "u16", "u32", "u64", "u128", "usize",
  "i8", "i16", "i32", "i64", "i128", "isize", "f32", "f64",
  "String", "Option", "Result", "Some", "None", "Ok", "Err", "Box",
  "Vec", "Rc", "Arc", "Cell", "RefCell", "Pin", "NonNull",
  "KernelResult", "Error", "CStr", "CString", "SpinLock", "Mutex",
  "Ref", "ScopeGuard", "KBox"
]);

export const rustGrammar = {
  id: "rust",
  name: "Rust",
  ftypes: [3],
  extensions: [".rs"],
  filenames: [],
  shebangs: [],
  keywords: RUST_KEYWORDS,
  types: RUST_TYPES,
  hasPreprocessor: false,
  supportsInclude: true,
  isCommentLine(trimmed) {
    return trimmed.startsWith("//") || trimmed.startsWith("/*") || (trimmed.startsWith("*") && !trimmed.startsWith("*="));
  },
  isType(val) {
    if (RUST_TYPES.has(val)) return true;
    // Rust convention: UpperCamelCase identifiers are types/traits/enums
    return /^[A-Z][a-zA-Z0-9_]*$/.test(val);
  }
};

