"""parser/kconfig_ast/kconfig_toolchain.py - Toolchain profile answering Kconfig `$(shell,...)` calls.

Since Linux 4.18, Kconfig asks the build toolchain questions while it is parsed: compiler and
linker versions (cc-version.sh, ld-version.sh...), flag support (`$(cc-option,...)`,
`$(ld-option,...)`, `$(as-instr,...)`), small compile tests, helper scripts. The real kconfig runs
those commands; the web application cannot, so ToolchainShell answers them from a profile:

- **Identity** (`cc`/`cc_version`, `ld`/`ld_version`, `as`, pahole, rustc): drives the version
  scripts with each era's exact output format, the environment the top Makefile exports (CC, LD,
  CC_VERSION_TEXT, CLANG_FLAGS, ...) and the minimum-version checks of scripts/min-tool-version.sh.
- **Capability knowledge**: a table of well-known flags/features with the first gcc/clang
  versions that support them. Anything else is *assumed* supported (and reported as such).
- **Overrides**: `env` replaces any environment variable Kconfig reads, `probes` forces the answer
  of a probe (`"y"`/`"n"` for checks, text for commands whose output is used).

Every answer is recorded (ToolchainShell.probes) with its source, so the user can review and
override what decided the configuration. A small POSIX shell subset is interpreted (`{ }`, `&&`,
`||`, `;`, pipes, redirections, `test`, `echo`, `printf`, `set --`, `grep`, `head`, `sed`, `cut`,
`command -v`, `env`, `uname`), and external commands are classified into probes.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------------------------------

# Toolchain contemporary with a kernel release: (kernel version, gcc, binutils, clang, pahole)
_ERA_DEFAULTS = [
    ((4, 0), "5.4.0", "2.25", "3.8.0", ""),
    ((4, 15), "7.3.0", "2.29", "6.0.0", ""),
    ((5, 0), "8.3.0", "2.32", "8.0.0", "1.15"),
    ((5, 4), "9.2.0", "2.33", "9.0.0", "1.16"),
    ((5, 10), "10.2.0", "2.35", "11.0.0", "1.19"),
    ((5, 15), "11.2.0", "2.37", "13.0.0", "1.22"),
    ((6, 0), "12.2.0", "2.39", "15.0.0", "1.24"),
    ((6, 6), "13.2.0", "2.41", "17.0.0", "1.25"),
    ((6, 12), "14.2.0", "2.43", "19.0.0", "1.27"),
]


def _parse_version(text: str | None) -> tuple[int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", text or "")[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _canonical(text: str | None) -> int:
    major, minor, patch = _parse_version(text)
    return 10000 * major + 100 * minor + patch


@dataclass
class ToolchainProfile:
    cc: str = "gcc"                 # gcc | clang
    cc_version: str = ""
    ld: str = ""                    # bfd | lld (default: bfd with gcc, lld with clang)
    ld_version: str = ""            # binutils version for bfd, LLVM version for lld
    assembler: str = ""             # gnu | llvm (integrated) (default: gnu with gcc, llvm with clang)
    binutils_version: str = ""      # GNU as / objcopy / nm version
    cross_compile: str = ""         # CROSS_COMPILE prefix
    llvm_utils: bool | None = None  # LLVM=1 (llvm-ar, llvm-nm, llvm-objcopy...); default: clang
    pahole_version: str | None = None  # "" = pahole not installed; None = era default
    rustc_version: str = ""         # "" = Rust toolchain not available
    gcc_plugins: bool = False       # gcc plugin development headers installed
    host_release: str = "6.8.0"     # `uname -r` of the build machine
    env: dict[str, str] = field(default_factory=dict)
    probes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, kernelversion: str = "") -> "ToolchainProfile":
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}
        prof = cls(**{k: v for k, v in data.items() if k in known})
        for key in ("pahole_version", "rustc_version"):
            if isinstance(getattr(prof, key), str) and getattr(prof, key).strip().lower() == "none":
                setattr(prof, key, "")  # explicitly not installed
        prof.env = {str(k): str(v) for k, v in (prof.env or {}).items()}
        prof.probes = {str(k): str(v) for k, v in (prof.probes or {}).items()}
        prof.fill_defaults(kernelversion)
        return prof

    def fill_defaults(self, kernelversion: str) -> None:
        kv = _parse_version(kernelversion)[:2]
        era = _ERA_DEFAULTS[0]
        for row in _ERA_DEFAULTS:
            if kv >= row[0]:
                era = row
        _, gcc_v, binutils_v, clang_v, pahole_v = era
        self.cc = "clang" if self.cc == "clang" else "gcc"
        if not self.cc_version:
            self.cc_version = clang_v if self.cc == "clang" else gcc_v
        if self.ld not in ("bfd", "lld"):
            self.ld = "lld" if self.cc == "clang" else "bfd"
        if not self.binutils_version:
            self.binutils_version = binutils_v
        if not self.ld_version:
            self.ld_version = self.cc_version if self.ld == "lld" else self.binutils_version
        if self.assembler not in ("gnu", "llvm"):
            self.assembler = "llvm" if self.cc == "clang" else "gnu"
        if self.llvm_utils is None:
            self.llvm_utils = self.cc == "clang"
        if self.pahole_version is None:
            self.pahole_version = pahole_v

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------------ derived identity
    @property
    def is_clang(self) -> bool:
        return self.cc == "clang"

    def tool(self, gnu: str, llvm: str) -> str:
        return llvm if self.llvm_utils else f"{self.cross_compile}{gnu}"

    def environment(self, arch: str, srcarch: str, kernelversion: str) -> dict[str, str]:
        """Variables the top Makefile exports to kconfig, then the user's overrides."""
        cc = "clang" if self.is_clang else f"{self.cross_compile}gcc"
        ld = "ld.lld" if self.ld == "lld" else f"{self.cross_compile}ld"
        clang_flags = ""
        if self.is_clang:
            parts = []
            if self.cross_compile:
                parts.append(f"--target={self.cross_compile.rstrip('-')}")
            parts.append("-fintegrated-as" if self.assembler == "llvm" else "-fno-integrated-as")
            clang_flags = " ".join(parts)
        env = {
            "ARCH": arch,
            "SRCARCH": srcarch,
            "KERNELVERSION": kernelversion,
            "srctree": ".",
            "objtree": ".",
            "CC": cc,
            "LD": ld,
            "AR": self.tool("ar", "llvm-ar"),
            "NM": self.tool("nm", "llvm-nm"),
            "OBJCOPY": self.tool("objcopy", "llvm-objcopy"),
            "OBJDUMP": self.tool("objdump", "llvm-objdump"),
            "STRIP": self.tool("strip", "llvm-strip"),
            "HOSTCC": "clang" if self.is_clang else "gcc",
            "HOSTCXX": "clang++" if self.is_clang else "g++",
            "CROSS_COMPILE": self.cross_compile,
            "CLANG_FLAGS": clang_flags,
            "CC_VERSION_TEXT": self.cc_version_text(),
            "PAHOLE": "pahole" if self.pahole_version else "",
            "RUSTC": "rustc" if self.rustc_version else "",
            "BINDGEN": "bindgen" if self.rustc_version else "",
            "LLVM": "1" if self.llvm_utils else "",
            "LLVM_IAS": "1" if self.is_clang and self.assembler == "llvm" else "",
            "USERCFLAGS": "",
            "USERLDFLAGS": "",
            "CROSS_COMPILE_COMPAT": "",
        }
        env.update(self.env)
        return env

    def cc_version_text(self) -> str:
        if self.is_clang:
            return f"clang version {self.cc_version}"
        return f"{self.cross_compile}gcc (GCC) {self.cc_version}"

    def ld_version_text(self) -> str:
        if self.ld == "lld":
            return f"LLD {self.ld_version} (compatible with GNU linkers)"
        return f"GNU ld (GNU Binutils) {self.ld_version}"

    def summary(self) -> str:
        return f"{self.cc} {self.cc_version} · {'LLD' if self.ld == 'lld' else 'GNU ld'} {self.ld_version}"


# --------------------------------------------------------------------------------------------------
# Capability knowledge (first supporting versions; None = never). Approximate, overridable.
# --------------------------------------------------------------------------------------------------

_FLAG_SUPPORT: list[tuple[str, str | None, str | None, Any]] = [
    # (flag regex, gcc min, clang min, architectures: () = any, a tuple for both compilers, or
    #  {"gcc": (...), "clang": (...)} when only one of them is target-restricted)
    (r"-D__SIZEOF_INT128__=0$", None, None, ()),  # predefined by 64-bit compilers: redefinition fails
    (r"^-m(32|64)$", "4.0", "3.0", ("x86", "sparc", "powerpc", "s390", "mips")),
    (r"^-fcf-protection=branch -mindirect-branch-register$", "8.0", None, ("x86",)),
    (r"^-fcf-protection(=\w+)?$", "8.0", "7.0", ("x86",)),
    (r"^-mindirect-branch-register$", "8.0", None, ("x86",)),
    (r"^-mindirect-branch=thunk-extern", "8.0", None, ("x86",)),
    (r"^-mfunction-return=thunk-extern$", "8.0", "15.0", ("x86",)),
    (r"^-mharden-sls=all$", "12.0", "15.0", ("x86", "arm64", "arm")),
    (r"^-mrecord-mcount$", "5.0", None, ("x86", "s390")),
    (r"^-mfentry$", "4.6", "5.0", ("x86", "s390")),
    (r"^-mbranch-protection=", "9.0", "8.0", ("arm64",)),
    (r"^-fsanitize=kcfi$", None, "16.0", ()),
    (r"^-ftrivial-auto-var-init=pattern$", "12.0", "8.0", ()),
    (r"^-ftrivial-auto-var-init=zero$", "12.0", "16.0", ()),
    # gcc reads "-enable-..." as "-e nable-..." (a link-time entry point), ignored with -c
    (r"^-ftrivial-auto-var-init=zero -enable-trivial-auto-var-init-zero-knowing-it-will-be-removed-from-clang$", "12.0", "8.0", ()),
    (r"^-fzero-call-used-regs=", "11.0", "15.0", ("x86", "arm64")),
    (r"^-frandomize-layout-seed-file=", None, "15.0", ()),
    (r"^-fsanitize=kernel-address$", "5.0", "3.9", ()),
    (r"^-fsanitize=kernel-hwaddress$", "11.0", "7.0", {"gcc": ("arm64",), "clang": ()}),
    (r"^-fsanitize=kernel-memory", None, "14.0", ()),
    (r"^-fsanitize=thread --param tsan-distinguish-volatile=1$", "11.0", None, ()),
    (r"^-fsanitize=thread --param", None, None, ()),
    (r"^-fsanitize=thread -mllvm tsan-distinguish-volatile=1$", None, "11.0", ()),
    (r"^-fsanitize=thread -mllvm", None, "12.0", ()),
    (r"^-fsanitize=(array-bounds|local-bounds)$", None, "3.0", ()),
    (r"^-fsanitize=bounds-strict$", "6.0", None, ()),
    (r"^-fsanitize=(bounds|shift|integer-divide-by-zero|unreachable|bool|enum|alignment|signed-integer-overflow|object-size)$", "5.0", "3.0", ()),
    (r"^-fsanitize-undefined-trap-on-error$", "5.0", "3.0", ()),
    (r"^-fsanitize-coverage=trace-pc$", "6.0", "4.0", ()),
    (r"^-fsanitize-coverage=trace-cmp$", "8.0", "4.0", ()),
    (r"^-Wimplicit-fallthrough=\d$", "7.0", None, ()),
    (r"^-Wunreachable-code-fallthrough$", None, "14.0", ()),
    (r"^-fstack-protector(-strong|-all)?$", "4.9", "3.5", ()),
    (r"^-fno-stack-protector$", "4.0", "3.0", ()),
    (r"^-mstack-protector-guard", "8.0", "13.0", ()),
    (r"^-gz=zlib$", "5.0", "5.0", ()),
    (r"^-gz=zstd$", "13.0", "16.0", ()),
    (r"^-gsplit-dwarf$", "4.7", "3.5", ()),
    (r"^-gdwarf-(4|5)$", "7.0", "7.0", ()),
    (r"^-fpatchable-function-entry=", "8.0", "10.0", ()),
    (r"^-fmin-function-alignment=", "14.0", None, ()),
    (r"^-fstrict-flex-arrays", "13.0", "16.0", ()),
    (r"^-fsanitize=shadow-call-stack$", None, "7.0", ("arm64",)),
]

_SNIPPET_SUPPORT: list[tuple[str, str | None, str | None]] = [
    # (C snippet regex, gcc min, clang min)
    (r"asm goto \(\"\"\s*:\s*\"=r\"", "11.0", "11.0"),                # asm goto with outputs
    (r"asm goto \(\"\.long \(%l\[bar\]\) - \.\"\s*:\s*\"\+m\"", "11.0", "17.0"),  # tied outputs
    (r"asm goto", "4.5", "9.0"),
    (r"asm inline", "9.0", "11.0"),
    (r"no_profile_instrument_function", "7.0", "13.0"),
    (r"__attribute__\(\(__counted_by__", "15.0", "18.0"),
]


def _supported(min_gcc: str | None, min_clang: str | None, prof: ToolchainProfile) -> bool:
    need = min_clang if prof.is_clang else min_gcc
    return need is not None and _parse_version(prof.cc_version) >= _parse_version(need)


# --------------------------------------------------------------------------------------------------
# Shell interpreter
# --------------------------------------------------------------------------------------------------

class _Exit(Exception):
    pass


_OPS = {";", "&&", "||", "|", "&", ">", ">>", "<", ">&", "<&", "&>", "(", ")"}


class ToolchainShell:
    """Callable used as KconfigPreprocessor.shell: answers commands from a ToolchainProfile."""

    def __init__(self, profile: ToolchainProfile, env: dict[str, str], srcarch: str,
                 read_file: Callable[[str], str | None] | None = None) -> None:
        self.profile = profile
        self.env = env
        self.srcarch = srcarch
        self.read_file = read_file or (lambda path: None)
        self.pp: Any = None  # set by the caller to record locations
        self.probes: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ recording
    def _record(self, key: str, kind: str, label: str, command: str, result: str, source: str, note: str = "") -> str:
        entry = self.probes.get(key)
        if entry is None:
            entry = self.probes[key] = {
                "key": key, "kind": kind, "label": label, "command": command, "result": result,
                "source": source, "note": note, "locations": [],
            }
        if self.pp is not None:
            loc = {"file": self.pp.file, "line": self.pp.lineno}
            if loc not in entry["locations"]:
                entry["locations"].append(loc)
        return result

    def _decide(self, key: str, kind: str, label: str, command: str, decision: tuple[bool, str, str]) -> bool:
        """Boolean probe: override > knowledge/toolchain decision; returns success."""
        if key in self.profile.probes:
            value = self.profile.probes[key].strip().lower()
            ok = value in ("y", "yes", "1", "true")
            self._record(key, kind, label, command, "y" if ok else "n", "override")
            return ok
        ok, source, note = decision
        self._record(key, kind, label, command, "y" if ok else "n", source, note)
        return ok

    def _output(self, key: str, kind: str, label: str, command: str, value: str, source: str, note: str = "") -> str:
        """Output probe: override > computed value; returns stdout text (without trailing newline)."""
        if key in self.profile.probes:
            return self._record(key, kind, label, command, self.profile.probes[key], "override")
        return self._record(key, kind, label, command, value, source, note)

    # ------------------------------------------------------------------ entry point
    def __call__(self, command: str) -> str:
        try:
            _, out = self._run_list(self._tokens(command), "", {})
        except (_Exit, ValueError, IndexError) as exc:
            if isinstance(exc, ValueError):
                self._record(f"cmd:{command}", "unparsed", "Unparsed shell command", command, "", "assumed", str(exc))
            return ""
        return out

    def _tokens(self, command: str) -> list[str]:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)

    # ------------------------------------------------------------------ grammar: list / and-or / pipeline
    def _run_list(self, toks: list[str], stdin: str, pos_args: dict[str, str]) -> tuple[int, str]:
        status, out = 0, ""
        for part in self._split(toks, (";", "&")):
            if part:
                status, o = self._run_and_or(part, stdin, pos_args)
                out += o
        return status, out

    def _split(self, toks: list[str], seps: tuple[str, ...]) -> list[list[str]]:
        parts: list[list[str]] = [[]]
        depth = 0
        for tok in toks:
            if tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
            if depth == 0 and tok in seps:
                parts.append([])
            else:
                parts[-1].append(tok)
        return parts

    def _run_and_or(self, toks: list[str], stdin: str, pos_args: dict[str, str]) -> tuple[int, str]:
        segments: list[tuple[str, list[str]]] = [("", [])]
        depth = 0
        for tok in toks:
            if tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
            if depth == 0 and tok in ("&&", "||"):
                segments.append((tok, []))
            else:
                segments[-1][1].append(tok)
        status, out = 0, ""
        for op, seg in segments:
            if (op == "&&" and status != 0) or (op == "||" and status == 0):
                continue
            status, o = self._run_pipeline(seg, stdin, pos_args)
            out += o
        return status, out

    def _run_pipeline(self, toks: list[str], stdin: str, pos_args: dict[str, str]) -> tuple[int, str]:
        status, data = 0, stdin
        for cmd in self._split(toks, ("|",)):
            status, data = self._run_command(cmd, data, pos_args)
        return status, data

    def _run_command(self, toks: list[str], stdin: str, pos_args: dict[str, str]) -> tuple[int, str]:
        if toks and toks[0] == "{":
            end = len(toks) - 1 - toks[::-1].index("}")
            status, out = self._run_list(toks[1:end], stdin, pos_args)
            return status, ("" if self._redirects_stdout(toks[end + 1:]) else out)
        words: list[str] = []
        silent = False
        i = 0
        while i < len(toks):
            tok = toks[i]
            if tok in (">", ">>", "<", ">&", "<&", "&>"):
                target = toks[i + 1] if i + 1 < len(toks) else ""
                if words and words[-1] in ("1", "2") and tok != "<":
                    fd = words.pop()
                else:
                    fd = "1"
                if fd == "1" and tok in (">", ">>", "&>") and target == "/dev/null":
                    silent = True
                i += 2
                continue
            words.append(self._expand(tok, pos_args))
            i += 1
        status, out = self._simple(words, stdin, pos_args)
        return status, ("" if silent else out)

    @staticmethod
    def _redirects_stdout(toks: list[str]) -> bool:
        return any(t in (">", ">>", "&>") and i + 1 < len(toks) and toks[i + 1] == "/dev/null" for i, t in enumerate(toks))

    def _expand(self, word: str, pos_args: dict[str, str]) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1) or m.group(2)
            if name.isdigit() or name == "#":
                return pos_args.get(name, "")
            if name == "$":
                return "4242"
            return self.env.get(name, "")
        return re.sub(r"\$\{(\w+|#)\}|\$(\d|#|\$|[A-Za-z_]\w*)", repl, word)

    # ------------------------------------------------------------------ simple commands
    def _simple(self, words: list[str], stdin: str, pos_args: dict[str, str]) -> tuple[int, str]:
        if not words:
            return 0, ""
        while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
            words = words[1:]  # VAR=value prefixes do not matter to the probes we answer
        if not words:
            return 0, ""
        name, args = words[0], words[1:]
        if name == "":
            return 127, ""
        builtin = getattr(self, f"_b_{name.replace('-', '_').replace('[', 'test')}", None)
        if builtin is not None:
            return builtin(args, stdin, pos_args)
        return self._external(words, stdin)

    def _b_true(self, args, stdin, pos):
        return 0, ""

    def _b_false(self, args, stdin, pos):
        return 1, ""

    _b_trap = _b_true
    _b_mkdir = _b_true
    _b_rm = _b_true
    _b_exit = _b_true

    def _b_echo(self, args, stdin, pos):
        newline = True
        if args and args[0] == "-n":
            newline, args = False, args[1:]
        return 0, " ".join(args) + ("\n" if newline else "")

    def _b_printf(self, args, stdin, pos):
        if not args:
            return 0, ""
        fmt, rest = args[0], args[1:]

        def esc(text: str) -> str:
            return text.encode("utf-8").decode("unicode_escape", errors="ignore")

        out = re.sub(r"%[bsd]", lambda m: (esc(rest.pop(0)) if m.group(0) == "%b" else rest.pop(0)) if rest else "", fmt)
        return 0, esc(out)

    def _b_cat(self, args, stdin, pos):
        return 0, stdin

    def _b_set(self, args, stdin, pos):
        if args and args[0] == "--":
            args = args[1:]
        words = " ".join(args).split()
        for key in [k for k in pos if k.isdigit()]:
            del pos[key]
        for i, w in enumerate(words, 1):
            pos[str(i)] = w
        pos["#"] = str(len(words))
        return 0, ""

    def _b_uname(self, args, stdin, pos):
        value = self.profile.host_release if "-r" in args else "Linux"
        return 0, self._output("cmd:uname " + " ".join(args), "host", "Build host kernel (uname)", "uname " + " ".join(args), value, "toolchain") + "\n"

    def _b_command(self, args, stdin, pos):
        if args and args[0] == "-v":
            tool = args[1] if len(args) > 1 else ""
            return (0, tool + "\n") if tool else (1, "")
        return self._simple(args, stdin, pos)

    def _b_env(self, args, stdin, pos):
        while args and "=" in args[0]:
            args = args[1:]
        return self._simple(args, stdin, pos)

    def _b_grep(self, args, stdin, pos):
        flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
        patterns = [a for a in args if not a.startswith("-")]
        pattern = patterns[0] if patterns else ""
        lines = stdin.splitlines()
        regex = re.compile(pattern, re.IGNORECASE if "i" in flags else 0)
        hits = [ln for ln in lines if bool(regex.search(ln)) != ("v" in flags)]
        return (0 if hits else 1), ("" if "q" in flags else "".join(h + "\n" for h in hits))

    def _b_head(self, args, stdin, pos):
        n = 10
        for i, a in enumerate(args):
            if a == "-n" and i + 1 < len(args):
                n = int(args[i + 1])
            elif re.match(r"^-n?\d+$", a):
                n = int(a.lstrip("-n"))
        return 0, "".join(line + "\n" for line in stdin.splitlines()[:n])

    def _b_tail(self, args, stdin, pos):
        n = 10
        for i, a in enumerate(args):
            if a == "-n" and i + 1 < len(args):
                n = int(args[i + 1])
            elif re.match(r"^-n?\d+$", a):
                n = int(a.lstrip("-n"))
        return 0, "".join(line + "\n" for line in stdin.splitlines()[-n:])

    def _b_sed(self, args, stdin, pos):
        out = stdin
        for script in [a for a in args if a.startswith("s")]:
            sep = script[1]
            parts = script.split(sep)
            if len(parts) >= 3:
                pattern = parts[1].replace(r"\(", "(").replace(r"\)", ")") if "-E" not in args else parts[1]
                repl = re.sub(r"\\(\d)", r"\\\1", parts[2])
                count = 0 if "g" in (parts[3] if len(parts) > 3 else "") else 1
                out = "".join(re.sub(pattern, repl, line, count=count) + "\n" for line in out.splitlines())
        return 0, out

    def _b_cut(self, args, stdin, pos):
        delim, fields = "\t", "1"
        for i, a in enumerate(args):
            if a.startswith("-d"):
                delim = a[2:] or args[i + 1]
            elif a.startswith("-f"):
                fields = a[2:] or args[i + 1]
        idx = int(fields.split(",")[0]) - 1
        out = []
        for line in stdin.splitlines():
            cols = line.split(delim)
            out.append(cols[idx] if idx < len(cols) else "")
        return 0, "".join(o + "\n" for o in out)

    def _b_test(self, args, stdin, pos):
        if args and args[-1] == "]":
            args = args[:-1]
        return (0 if self._test(args) else 1), ""

    def _test(self, args: list[str]) -> bool:
        if "-o" in args:
            i = args.index("-o")
            return self._test(args[:i]) or self._test(args[i + 1:])
        if "-a" in args:
            i = args.index("-a")
            return self._test(args[:i]) and self._test(args[i + 1:])
        if args and args[0] == "!":
            return not self._test(args[1:])
        if len(args) == 1:
            return args[0] != ""
        if len(args) == 2:
            op, val = args
            if op == "-z":
                return val == ""
            if op == "-n":
                return val != ""
            if op in ("-e", "-f", "-d", "-x", "-r", "-s"):
                key = f"file:{val}"
                plugin = "plugin" in val
                exists = self.profile.gcc_plugins and not self.profile.is_clang if plugin else False
                note = "gcc plugin headers (toolchain profile: gcc plugins)" if plugin else "host file: assumed absent"
                return self._decide(key, "file", f"File exists: {val}", f"test {op} {val}", (exists, "toolchain" if plugin else "assumed", note))
            return False
        if len(args) == 3:
            a, op, b = args
            if op in ("=", "=="):
                return a == b
            if op == "!=":
                return a != b
            try:
                x, y = int(a or "0"), int(b or "0")
            except ValueError:
                return False
            return {"-eq": x == y, "-ne": x != y, "-ge": x >= y, "-gt": x > y, "-le": x <= y, "-lt": x < y}.get(op, False)
        return False

    # ------------------------------------------------------------------ external commands
    def _external(self, words: list[str], stdin: str) -> tuple[int, str]:
        name = words[0]
        base = name.rsplit("/", 1)[-1]
        command = " ".join(words)
        cc_names = {self.env.get("CC", ""), "gcc", "clang", "cc", f"{self.profile.cross_compile}gcc"}
        ld_names = {self.env.get("LD", ""), "ld", "ld.lld", f"{self.profile.cross_compile}ld"}
        if name in cc_names or base.endswith("gcc") or base.startswith("clang"):
            return self._compiler(words, stdin, command)
        if name in ld_names or base in ("ld", "ld.lld", "ld.bfd"):
            return self._linker(words, command)
        if "/scripts/" in f"/{name}" or name.startswith("scripts/"):
            return self._script(name.split("scripts/", 1)[1], words[1:], stdin, command)
        if any(name == self.env.get(t) for t in ("AR", "NM", "OBJCOPY", "OBJDUMP", "STRIP")) or base.startswith("llvm-"):
            tool = base.replace("llvm-", "")
            text = f"LLVM (http://llvm.org/):\n  LLVM version {self.profile.cc_version}" if self.profile.llvm_utils else f"GNU {tool} (GNU Binutils) {self.profile.binutils_version}"
            return 0, text + "\n"
        if base in ("rustc", "bindgen"):
            if not self.profile.rustc_version:
                return 127, ""
            return 0, f"{base} {self.profile.rustc_version}\n"
        if base == "pahole":
            return (0, f"v{self.profile.pahole_version}\n") if self.profile.pahole_version else (127, "")
        ok = self._decide(f"cmd:{command}", "command", f"Command: {command[:80]}", command, (True, "assumed", "unknown command: assumed to succeed"))
        return (0 if ok else 1), ""

    def _compiler(self, words: list[str], stdin: str, command: str) -> tuple[int, str]:
        prof = self.profile
        args = words[1:]
        if "--version" in args:
            text = prof.cc_version_text() + ("\nTarget: x86_64-unknown-linux-gnu" if prof.is_clang else "\nCopyright (C) Free Software Foundation, Inc.")
            return 0, text + "\n"
        if "-dumpversion" in args:
            return 0, prof.cc_version + "\n"
        printed = next((a for a in args if a.startswith("-print-file-name=")), None)
        if printed:
            target = printed.split("=", 1)[1]
            major = _parse_version(prof.cc_version)[0]
            value = target if prof.is_clang else f"/usr/lib/gcc/{prof.cross_compile.rstrip('-') or 'x86_64-linux-gnu'}/{major}/{target}"
            return 0, self._output(f"cc-print:{target}", "compiler", f"Compiler file {target}", command, value, "toolchain") + "\n"
        clang_flags = set(self.env.get("CLANG_FLAGS", "").split())
        flags: list[str] = []
        skip = False
        language = ""
        for i, a in enumerate(args):
            if skip:
                skip = False
                continue
            if a in ("-o", "-x"):
                if a == "-x" and i + 1 < len(args):
                    language = args[i + 1]
                skip = True
                continue
            if a in ("-Werror", "-c", "-E", "-S", "/dev/null", "-") or a in clang_flags or a.startswith(".tmp_"):
                continue
            flags.append(a)
        if language.startswith("assembler"):
            instr = stdin.strip()
            key = f"as-instr:{instr}"
            return (0 if self._decide(key, "as-instr", f"Assembler accepts: {instr[:70]}", command,
                                      (True, "assumed", "assembler instructions are assumed supported")) else 1), ""
        if "-" in args and stdin.strip():
            code = stdin.strip()
            key = f"cc-snippet:{code}" + (f" {' '.join(flags)}" if flags else "")
            decision = (True, "assumed", "compile test assumed to succeed")
            for pattern, gcc_min, clang_min in _SNIPPET_SUPPORT:
                if re.search(pattern, code):
                    decision = (_supported(gcc_min, clang_min, prof), "knowledge",
                                f"gcc >= {gcc_min or 'never'}, clang >= {clang_min or 'never'}")
                    break
            return (0 if self._decide(key, "cc-compile", f"Compiles: {code[:70]}", command, decision) else 1), ""
        flag_text = " ".join(flags)
        key = f"cc-option:{flag_text}"
        decision = (True, "assumed", "compiler flag assumed to be supported")
        for pattern, gcc_min, clang_min, archs in _FLAG_SUPPORT:
            if re.search(pattern, flag_text):
                if isinstance(archs, dict):
                    archs = archs.get("clang" if prof.is_clang else "gcc", ())
                ok = _supported(gcc_min, clang_min, prof) and (not archs or self.srcarch in archs)
                note = f"gcc >= {gcc_min or 'never'}, clang >= {clang_min or 'never'}" + (f", only {'/'.join(archs)}" if archs else "")
                decision = (ok, "knowledge", note)
                break
        else:
            if "-mllvm" in flags:
                decision = (prof.is_clang, "knowledge", "-mllvm options are clang-only")
            elif "--param" in flags:
                decision = (not prof.is_clang, "knowledge", "--param options are gcc-only")
        return (0 if self._decide(key, "cc-option", f"Compiler accepts {flag_text}", command, decision) else 1), ""

    def _linker(self, words: list[str], command: str) -> tuple[int, str]:
        args = words[1:]
        if args in (["-v"], ["--version"], ["-V"]):
            return 0, self.profile.ld_version_text() + "\n"
        flags = " ".join(a for a in args if a not in ("-v", "--version"))
        key = f"ld-option:{flags}"
        ok = self._decide(key, "ld-option", f"Linker accepts {flags}", command, (True, "assumed", "linker flag assumed to be supported"))
        return (0 if ok else 1), (self.profile.ld_version_text() + "\n" if ok and "-v" in args else "")

    # ------------------------------------------------------------------ scripts/*.sh
    def _script(self, name: str, args: list[str], stdin: str, command: str) -> tuple[int, str]:
        prof = self.profile
        src = self.read_file(f"scripts/{name}") or ""
        cc_major, cc_minor, cc_patch = _parse_version(prof.cc_version)
        gnuc = (4, 2, 1) if prof.is_clang else (cc_major, cc_minor, cc_patch)

        def out(value: str, note: str = "") -> tuple[int, str]:
            text = self._output(f"script:{name}", "version", f"scripts/{name}", command, value, "toolchain", note)
            return (0 if text != "" else 1), (text + "\n" if text else "")

        if name == "cc-version.sh":
            low = self._below_min("llvm" if prof.is_clang else "gcc", prof.cc_version)
            if low:
                return out("", low)
            return out(f"{'Clang' if prof.is_clang else 'GCC'} {_canonical(prof.cc_version)}")
        if name == "gcc-version.sh":
            if "-p" in args and "%02d%02d%02d" in src:
                return out(f"{gnuc[0]:02d}{gnuc[1]:02d}{gnuc[2]:02d}")
            if "%d%02d%02d" in src:
                return out(f"{gnuc[0]}{gnuc[1]:02d}{gnuc[2]:02d}")
            return out(f"{gnuc[0]:02d}{gnuc[1]:02d}")
        if name == "clang-version.sh":
            return out(f"{cc_major}{cc_minor:02d}{cc_patch:02d}" if prof.is_clang else "0")
        if name == "lld-version.sh":
            if prof.ld != "lld":
                return out("0")
            major, minor, patch = _parse_version(prof.ld_version)
            return out(f"{major}{minor:02d}{patch:02d}")
        if name == "ld-version.sh":
            if "BFD" in src:  # 5.13+: "<name> <canonical version>"
                kind = "LLD" if prof.ld == "lld" else "BFD"
                low = self._below_min("llvm" if prof.ld == "lld" else "binutils", prof.ld_version)
                return out("" if low else f"{kind} {_canonical(prof.ld_version)}", low)
            major, minor, patch = _parse_version(prof.ld_version)  # awk variant reading `ld --version`
            return out(str(major * 100000000 + minor * 1000000 + patch * 10000))
        if name == "as-version.sh":
            if prof.assembler == "llvm" or "-fintegrated-as" in args:
                return out("LLVM 0")
            low = self._below_min("binutils", prof.binutils_version)
            return out("" if low else f"GNU {_canonical(prof.binutils_version)}", low)
        if name == "pahole-version.sh":
            if not prof.pahole_version:
                return out("0", "pahole not installed (toolchain profile)")
            major, minor, _ = _parse_version(prof.pahole_version)
            return out(f"{major}{minor:02d}")
        if name in ("rustc-version.sh", "rustc-llvm-version.sh"):
            return out(str(_canonical(prof.rustc_version)) if prof.rustc_version else "0")
        if name == "gcc-plugin.sh" and args and not stdin and "-" not in args[-1] and len(args) >= 3:
            # 4.x variant prints the plugin compiler when plugins can be built
            return out(args[0] or args[-1] if prof.gcc_plugins and not prof.is_clang else "", "gcc plugin headers (toolchain profile)")
        # The compiler/linker command itself is not part of the key, the remaining arguments are
        extra = [a for a in args if a and a not in (self.env.get("CC"), self.env.get("LD"))]
        key = f"script:{name}" + (f" {' '.join(extra)}" if extra else "")
        label = f"scripts/{name}{' ' + ' '.join(extra) if extra else ''} succeeds"
        if name in ("gcc-plugin.sh",):
            decision = (prof.gcc_plugins and not prof.is_clang, "toolchain", "gcc plugin headers (toolchain profile)")
        elif name == "gcc-goto.sh":
            decision = (_supported("4.5", "9.0", prof), "knowledge", "asm goto: gcc >= 4.5, clang >= 9")
        elif name == "rust_is_available.sh":
            decision = (bool(prof.rustc_version), "toolchain", "Rust toolchain (toolchain profile)")
        elif name == "tools-support-relr.sh":
            decision = (prof.ld == "lld" or _parse_version(prof.ld_version) >= (2, 38, 0), "knowledge", "LLD, or GNU ld >= 2.38")
        elif name == "cc-can-link.sh":
            multilib = "-m32" in args or "-m31" in args
            decision = (not multilib, "assumed", "linking for the build host: native assumed to work, multilib (-m32) assumed missing")
        else:
            decision = (True, "assumed", "helper script assumed to succeed")
        ok = self._decide(key, "script", label, command, decision)
        return (0 if ok else 1), ""

    def _below_min(self, tool: str, version: str) -> str:
        """Message when scripts/min-tool-version.sh requires more than `version` (empty if fine)."""
        src = self.read_file("scripts/min-tool-version.sh") or ""
        block = re.search(rf"^{tool}\)\s*\n(.*?);;", src, re.M | re.S)
        if not block:
            return ""
        mins = re.findall(r"echo\s+([\d.]+)", block.group(1))
        if not mins:
            return ""
        minimum = mins[0] if tool != "llvm" or self.srcarch != "s390" or len(mins) < 2 else mins[0]
        if "s390" in block.group(1) and tool == "llvm" and self.srcarch != "s390" and len(mins) > 1:
            minimum = mins[1]
        if _parse_version(version) < _parse_version(minimum):
            return f"{tool} {version} is older than the minimum {minimum} (scripts/min-tool-version.sh)"
        return ""
