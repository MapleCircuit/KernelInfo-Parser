/**
 * assembly.js - Assembly language syntax grammar for kernel arch assembly code (.S, .s).
 */

export const ASM_KEYWORDS = new Set([
  "mov", "push", "pop", "call", "jmp", "je", "jne", "jz", "jnz", "ret",
  "leave", "add", "sub", "mul", "div", "xor", "and", "or", "not", "cmp",
  "test", "nop", "cli", "sti", "syscall", "sysret", "int", "iret", "lea",
  "inc", "dec", "shl", "shr", "sar", "rol", "ror", "bswap", "bt", "bts",
  "btr", "btc", "bsf", "bsr", "xchg", "cmpxchg", "lock", "rep", "repe",
  "repne", "movsb", "movsw", "movsl", "movsq", "stosb", "stosw", "stosl",
  "stosq", "cld", "std", "wrmsr", "rdmsr", "rdtsc", "cpuid", "in", "out",
  "hlt", "pause", "mfence", "lfence", "sfence", "clflush", "invlpg"
]);

export const ASM_DIRECTIVES = new Set([
  ".global", ".globl", ".section", ".text", ".data", ".bss", ".rodata",
  ".align", ".balign", ".p2align", ".long", ".word", ".byte", ".quad",
  ".ascii", ".asciz", ".string", ".type", ".size", ".macro", ".endm",
  ".rept", ".endr", ".file", ".ident", ".code32", ".code64", ".org",
  ".weak", ".set", ".equ", ".equiv", ".struct", ".comm", ".lcomm"
]);

export const assemblyGrammar = {
  id: "assembly",
  name: "Assembly",
  ftypes: [4],
  extensions: [".S", ".s", ".asm"],
  filenames: [],
  shebangs: [],
  keywords: ASM_KEYWORDS,
  types: new Set(),
  directives: ASM_DIRECTIVES,
  hasPreprocessor: true,
  supportsInclude: true,
  isCommentLine(trimmed) {
    return trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith(";") || trimmed.startsWith("@") ||
      (trimmed.startsWith("*") && !trimmed.startsWith("*=")) ||
      (trimmed.startsWith("#") && !/^\s*#\s*(include|define|undef|if|ifdef|ifndef|else|elif|endif|pragma)\b/.test(trimmed));
  },
  isType() {
    return false;
  }
};
