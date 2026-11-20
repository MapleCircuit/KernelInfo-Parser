"""parser/generated_headers.py - Headers a kernel build generates, written from the revision's own files.

A compilation of kernel code reads headers no Git tree holds: `make` writes them before compiling anything.
Without them libclang stops resolving the macros and types they carry (`CONFIG_*`, `TI_flags`, `__NR_read`,
`LINUX_VERSION_CODE`). `generated_headers(files, read, layout)` returns them as {repository path: text}, the
paths kbuild writes for that revision and ARCH:

- `autoconf.h` (`include/linux/autoconf.h` in 2.6.x, `include/generated/autoconf.h` from 2.6.33): the
  `make ARCH=<arch> defconfig` configuration, evaluated by `parser/kconfig_ast` (`KconfigEvaluator.load_config()`
  of the ARCH's default defconfig, then `set_all_new_symbols("default")`), in `conf_write_autoconf()` form.
- `asm-offsets.h` and `bounds.h`: the `DEFINE(NAME, ...)` / `OFFSET(NAME, ...)` / `BLANK()` names of
  `arch/<srcarch>/kernel/asm-offsets*.c` and `kernel/bounds.c`, each defined as 0. The values come from compiling
  those files, which no parse does: references resolve, value-dependent `#if` tests read 0.
- `version.h` (`LINUX_VERSION_CODE`, `KERNEL_VERSION()`), `utsrelease.h` (`UTS_RELEASE`) and `compile.h`
  from the top Makefile's version lines.
- x86 `unistd_32.h` / `unistd_64.h` / `unistd_x32.h` (`__NR_<name> <number>`) from the syscall tables
  (`arch/x86/entry/syscalls/syscall_*.tbl`, `arch/x86/syscalls/` before 4.2).
- `rq-offsets.h` names (`kernel/sched/rq-offsets.c`), x86 `asm/cpufeaturemasks.h` (every required/disabled
  feature mask 0), and empty stubs for the other generated headers kernel code includes (`timeconst.h`,
  `orc_hash.h`, ...), so their absence is no compilation error.

Pure: `read(path)` returns a tree file's text (None when absent); nothing is written here (`parser/compile_env.py`
places the files in the build directory of a revision and ARCH).
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

# Generated headers kernel code includes whose content no parse can compute: written empty
EMPTY_STUBS = (
    "include/generated/timeconst.h",
    "include/generated/autoksyms.h",
    "include/generated/rustc_cfg",
    "include/generated/vdso-offsets.h",
    "include/generated/vdso32-offsets.h",
    "include/linux/timeconst.h",
)
ARCH_EMPTY_STUBS = (
    "include/generated/asm/orc_hash.h",
    "include/generated/asm/syscalls_32.h",
    "include/generated/asm/syscalls_64.h",
    "include/generated/asm/syscalls_x32.h",
    "include/generated/asm/xen-hypercalls.h",
    "include/generated/uapi/asm/unistd_32.h",
    "include/generated/uapi/asm/unistd_64.h",
    "include/generated/uapi/asm/unistd_x32.h",
)

_OFFSET_NAME = re.compile(r"\b(?:DEFINE|OFFSET|COMMENT_DEFINE|STACK_OFFSET)\s*\(\s*([A-Za-z_]\w*)")
_BLANK_LIKE = re.compile(r"\b(?:ENTRY|CPUINFO_DEFINE|DEFINE_DEV)\s*\(\s*([A-Za-z_]\w*)")
_VERSION_LINE = re.compile(r"^(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)\s*=\s*(.*)$", re.M)


def kernel_release(makefile: str) -> tuple[int, int, int, str]:
    """(VERSION, PATCHLEVEL, SUBLEVEL, EXTRAVERSION) of the top Makefile (0 for a missing number)."""
    vals = {k: v.split("#", 1)[0].strip() for k, v in _VERSION_LINE.findall(makefile or "")}

    def num(key: str) -> int:
        m = re.match(r"\d+", vals.get(key, ""))
        return int(m.group(0)) if m else 0

    return num("VERSION"), num("PATCHLEVEL"), num("SUBLEVEL"), vals.get("EXTRAVERSION", "")


def _release_string(rel: tuple[int, int, int, str]) -> str:
    v, p, s, extra = rel
    return f"{v}.{p}.{s}{extra}" if v else ""


# ---------------------------------------------------------------------------------------------------- config
def default_defconfig(files: set[str] | Iterable[str], read: Callable[[str], str | None], arch: str, srcarch: str) -> str | None:
    """Tree path of the configuration `make ARCH=<arch> defconfig` loads: KBUILD_DEFCONFIG of
    arch/<srcarch>/Makefile in arch/<srcarch>/configs/, else arch/<srcarch>/defconfig."""
    from parser.kconfig_ast.kconfig_model import resolve_kbuild_defconfig

    names = files if isinstance(files, set) else set(files)
    name = resolve_kbuild_defconfig(read(f"arch/{srcarch}/Makefile") or "", arch, srcarch)
    if name and f"arch/{srcarch}/configs/{name}" in names:
        return f"arch/{srcarch}/configs/{name}"
    if f"arch/{srcarch}/defconfig" in names:
        return f"arch/{srcarch}/defconfig"
    if f"arch/{srcarch}/configs/{arch}_defconfig" in names:
        return f"arch/{srcarch}/configs/{arch}_defconfig"
    prefix = f"arch/{srcarch}/configs/"
    found = sorted(p for p in names if p.startswith(prefix) and p.endswith("defconfig"))
    return found[0] if found else None


def defconfig_values(files: set[str] | Iterable[str], read: Callable[[str], str | None], arch: str,
                     srcarch: str) -> tuple[dict[str, tuple[str, str]], str | None]:
    """({CONFIG name without prefix: (type, value)}, defconfig path) of `make ARCH=<arch> defconfig`.

    The Kconfig model is built like the webapp's Kconfig view does (`parser/kconfig_ast`, the default gcc
    toolchain profile for the macro language of 4.18+); an unreadable model gives ({}, None).
    """
    from parser.kconfig_ast.kconfig_eval import KconfigEvaluator
    from parser.kconfig_ast.kconfig_model import KconfigEnv, KconfigModelBuilder, kernel_version_string
    from parser.kconfig_ast.kconfig_preprocess import KconfigPreprocessor
    from parser.kconfig_ast.kconfig_toolchain import ToolchainProfile, ToolchainShell

    names = files if isinstance(files, set) else set(files)
    makefile = read("Makefile") or ""
    kv = kernel_version_string(makefile)
    defconfig = default_defconfig(names, read, arch, srcarch)
    try:
        profile = ToolchainProfile.from_dict({"cc": "gcc"}, kv)
        env_vars = profile.environment(arch, srcarch, kv)
        if defconfig:
            env_vars.setdefault("KBUILD_DEFCONFIG", defconfig.rsplit("/", 1)[-1])
        if arch == "um":
            env_vars.setdefault("SUBARCH", "x86")
        env = KconfigEnv(ARCH=env_vars["ARCH"], SRCARCH=env_vars["SRCARCH"], KERNELVERSION=env_vars["KERNELVERSION"], extra=env_vars)
        pp = None
        if "scripts/kconfig/preprocess.c" in names:
            shell = ToolchainShell(profile, env_vars, srcarch, read)
            pp = KconfigPreprocessor(env_vars, shell)
            shell.pp = pp
        root = "Kconfig" if "Kconfig" in names else f"arch/{srcarch}/Kconfig"
        model = KconfigModelBuilder(read, env, pp).build(root)
        ev = KconfigEvaluator(model)
        if defconfig:
            ev.load_config(read(defconfig) or "")
        ev.set_all_new_symbols("default")
        values = {name: (ev.get_type(name), value) for name, value in ev.config_values().items()}
    except Exception as exc:  # noqa: BLE001 - a revision whose Kconfig cannot be read compiles unconfigured
        logger.warning("ARCH=%s: defconfig evaluation failed: %s", arch, exc)
        return {}, None
    return values, defconfig


def autoconf_text(values: dict[str, tuple[str, str]]) -> str:
    """`include/generated/autoconf.h` of a configuration (scripts/kconfig confdata.c header printer)."""
    out = ["/* Generated by parser/generated_headers.py from the ARCH's defconfig */", "#define AUTOCONF_INCLUDED"]
    for name in sorted(values):
        sym_type, value = values[name]
        if sym_type in ("bool", "tristate"):
            if value == "y":
                out.append(f"#define CONFIG_{name} 1")
            elif value == "m":
                out.append(f"#define CONFIG_{name}_MODULE 1")
        elif sym_type == "string":
            esc = value.replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'#define CONFIG_{name} "{esc}"')
        elif sym_type in ("int", "hex") and value:
            if sym_type == "hex" and not value.lower().startswith("0x"):
                value = "0x" + value
            out.append(f"#define CONFIG_{name} {value}")
    return "\n".join(out) + "\n"


def config_digest(values: dict[str, tuple[str, str]]) -> str:
    """Short identity of a configuration (fact family 7, build directory names)."""
    h = hashlib.sha256()
    for name in sorted(values):
        sym_type, val = values[name]
        h.update(f"{name}:{sym_type}={val}\n".encode("utf-8", "surrogatepass"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------------------------- name stubs
def offset_names(text: str) -> list[str]:
    """Names an asm-offsets.c / bounds.c defines (`DEFINE(NAME, ...)`, `OFFSET(NAME, ...)`), in order."""
    seen: dict[str, None] = {}
    for m in _OFFSET_NAME.finditer(text or ""):
        seen.setdefault(m.group(1), None)
    for m in _BLANK_LIKE.finditer(text or ""):
        if m.group(1).isupper():
            seen.setdefault(m.group(1), None)
    return list(seen)


def _defines_text(names: Iterable[str], guard: str) -> str:
    body = "\n".join(f"#define {n} 0" for n in names)
    return f"#ifndef {guard}\n#define {guard}\n/* Generated by parser/generated_headers.py: names only (value 0) */\n{body}\n#endif\n"


def syscall_header(table: str, abis: tuple[str, ...], prefix: str = "__NR_") -> str:
    """`#define __NR_<name> <number>` of a syscall table's rows (`<nr> <abi> <name> [<entry>]`) of `abis`."""
    lines = []
    for raw in (table or "").split("\n"):
        line = raw.split("#", 1)[0].split()
        if len(line) >= 3 and line[0].isdigit() and line[1] in abis:
            lines.append(f"#define {prefix}{line[2]} {line[0]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------- all
def generated_headers(files: set[str] | Iterable[str], read: Callable[[str], str | None], arch: str, srcarch: str,
                      config: dict[str, tuple[str, str]] | None) -> dict[str, str]:
    """{repository path: text} of the generated headers of `make ARCH=<arch>` (see the module docstring).

    `config` is `defconfig_values()`'s mapping (None or empty: an empty configuration).
    """
    names = files if isinstance(files, set) else set(files)
    out: dict[str, str] = {}
    auto = autoconf_text(config or {})
    out["include/generated/autoconf.h"] = auto
    out["include/linux/autoconf.h"] = auto

    rel = kernel_release(read("Makefile") or "")
    v, p, s, _extra = rel
    code = (v << 16) + (p << 8) + min(s, 255)
    version = (f"#define LINUX_VERSION_CODE {code}\n#define KERNEL_VERSION(a,b,c) (((a) << 16) + ((b) << 8) + "
               f"((c) > 255 ? 255 : (c)))\n#define LINUX_VERSION_MAJOR {v}\n#define LINUX_VERSION_PATCHLEVEL {p}\n"
               f"#define LINUX_VERSION_SUBLEVEL {s}\n")
    for path in ("include/linux/version.h", "include/generated/uapi/linux/version.h"):
        if path not in names:
            out[path] = version
    uts = f'#define UTS_RELEASE "{_release_string(rel)}"\n'
    out["include/linux/utsrelease.h"] = uts
    out["include/generated/utsrelease.h"] = uts
    compile_h = ('#define UTS_MACHINE "' + arch + '"\n#define UTS_VERSION "#1"\n#define LINUX_COMPILE_BY "kip"\n'
                 '#define LINUX_COMPILE_HOST "kip"\n#define LINUX_COMPILER "kip"\n')
    out["include/linux/compile.h"] = compile_h
    out["include/generated/compile.h"] = compile_h

    # asm-offsets: arch/<srcarch>/**/asm-offsets*.c (2.6.x writes include/asm-<arch>/asm-offsets.h)
    offsets: list[str] = []
    arch_prefix = f"arch/{srcarch}/"
    for path in sorted(n for n in names if n.startswith(arch_prefix) and n.endswith(".c") and "/asm-offsets" in n):
        offsets += [n for n in offset_names(read(path) or "") if n not in offsets]
    if offsets:
        text = _defines_text(offsets, "__ASM_OFFSETS_H__")
        out["include/generated/asm-offsets.h"] = text
        for d in {srcarch, arch}:
            out[f"include/asm-{d}/asm-offsets.h"] = text
            out[f"include/asm-{d}/asm_offsets.h"] = text
    if "kernel/bounds.c" in names:
        text = _defines_text(offset_names(read("kernel/bounds.c") or ""), "__LINUX_BOUNDS_H__")
        out["include/generated/bounds.h"] = text
        out["include/linux/bounds.h"] = text
    if "kernel/sched/rq-offsets.c" in names:
        out["include/generated/rq-offsets.h"] = _defines_text(offset_names(read("kernel/sched/rq-offsets.c") or ""),
                                                              "__RQ_OFFSETS_H__")
    if srcarch == "x86":
        # 6.15+: required/disabled CPU feature masks generated from arch/x86/Kconfig.cpufeatures (all clear)
        masks = [f"#define {kind}_MASK{i} 0" for kind in ("REQUIRED", "DISABLED") for i in range(32)]
        out["arch/x86/include/generated/asm/cpufeaturemasks.h"] = (
            "#ifndef _ASM_X86_CPUFEATUREMASKS_H\n#define _ASM_X86_CPUFEATUREMASKS_H\n" + "\n".join(masks)
            + "\n#define REQUIRED_MASK_BIT_SET(x) 0\n#define DISABLED_MASK_BIT_SET(x) 0\n"
            + "#define REQUIRED_MASK_CHECK do { } while (0)\n#define DISABLED_MASK_CHECK do { } while (0)\n#endif\n")

    # x86 syscall numbers (generated uapi asm/unistd_*.h since 3.3)
    if srcarch == "x86":
        for tdir in ("arch/x86/entry/syscalls", "arch/x86/syscalls"):
            t32, t64 = f"{tdir}/syscall_32.tbl", f"{tdir}/syscall_64.tbl"
            if t64 in names or t32 in names:
                raw32 = read(t32) or ""
                raw64 = read(t64) or ""
                tables = {"unistd_32.h": (raw32, ("i386",)),
                          "unistd_64.h": (raw64, ("common", "64")),
                          "unistd_x32.h": (raw64, ("common", "x32"))}
                for name, (table, abis) in tables.items():
                    out[f"arch/x86/include/generated/uapi/asm/{name}"] = syscall_header(table, abis)
                # Kernel-internal variants (arch/x86/entry/syscalls/Makefile: prefix ia32_ / x32_)
                out["arch/x86/include/generated/asm/unistd_32_ia32.h"] = syscall_header(raw32, ("i386",), "__NR_ia32_")
                out["arch/x86/include/generated/asm/unistd_64_x32.h"] = syscall_header(raw64, ("x32",), "__NR_x32_")
                break

    for path in EMPTY_STUBS:
        out.setdefault(path, "")
    if srcarch:
        for path in ARCH_EMPTY_STUBS:
            out.setdefault(f"arch/{srcarch}/{path}", "")
    # A tree file always wins over a generated one (older trees keep some of these in Git)
    return {path: text for path, text in out.items() if path not in names}
