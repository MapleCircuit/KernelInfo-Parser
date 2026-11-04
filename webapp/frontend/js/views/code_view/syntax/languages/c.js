/**
 * c.js - C / C++ language syntax grammar for the Linux kernel.
 */

export const C_KEYWORDS = new Set([
  "auto", "break", "case", "char", "const", "continue", "default", "do",
  "double", "else", "enum", "extern", "float", "for", "goto", "if",
  "inline", "int", "long", "register", "restrict", "return", "short",
  "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
  "unsigned", "void", "volatile", "while", "_Bool", "_Complex", "_Imaginary",
  "__init", "__exit", "__user", "__kernel", "__iomem", "__percpu", "__rcu",
  "asmlinkage", "FASTCALL", "__attribute__", "__inline__", "__inline",
  "__asm__", "__volatile__", "likely", "unlikely", "NULL"
]);

export const C_TYPES = new Set([
  "size_t", "ssize_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
  "int8_t", "int16_t", "int32_t", "int64_t", "u8", "u16", "u32", "u64",
  "s8", "s16", "s32", "s64", "__u8", "__u16", "__u32", "__u64",
  "__s8", "__s16", "__s32", "__s64", "__be16", "__be32", "__be64",
  "__le16", "__le32", "__le64", "gfp_t", "phys_addr_t", "resource_size_t",
  "dma_addr_t", "loff_t", "atomic_t", "atomic64_t", "spinlock_t",
  "mutex", "rwsem", "list_head", "hlist_node", "bool"
]);

export const cGrammar = {
  id: "c",
  name: "C",
  ftypes: [1],
  extensions: [".c", ".h", ".i"],
  filenames: [],
  shebangs: [],
  keywords: C_KEYWORDS,
  types: C_TYPES,
  hasPreprocessor: true,
  supportsInclude: true,
  isCommentLine(trimmed) {
    return trimmed.startsWith("//") || trimmed.startsWith("/*") || (trimmed.startsWith("*") && !trimmed.startsWith("*="));
  },
  isType(val) {
    return C_TYPES.has(val) || val.endsWith("_t") || val.endsWith("_type");
  }
};

