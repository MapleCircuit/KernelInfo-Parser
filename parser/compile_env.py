"""parser/compile_env.py - The compilation a kernel build runs for one file, as libclang arguments.

`CompileEnv` turns the build layouts of a revision (`parser/arch_layout.py` `ArchLayout`, one per ARCH) into what
the C and assembly parsers pass to libclang, so a file reads the headers `make ARCH=<arch>` would give it:

- **ARCH of a file** (`arch_for()`): `path_arch()` of the path (`arch/<a>/...`, `include/asm-<a>/...`) mapped
  to an ARCH value of the revision; generic code takes the definition ARCH (`DEFAULT_ARCH`, x86_64, else i386,
  else the first ARCH). A blob is parsed once, for that ARCH.
- **Build directory** (`ArchEnv.build_dir`): per revision and ARCH, a directory outside the tree holding what a
  build creates before it compiles: the layout's links (`include/asm` -> `include/asm-i386/` in 2.6.x) as
  symlinks into the tree, its generated `asm-generic` wrappers (`#include <asm-generic/x.h>`), and the headers of
  `parser/generated_headers.py` (defconfig `autoconf.h`, name-only `asm-offsets.h`, `version.h`, syscall numbers).
  Each include directory of the layout is searched in the tree, then at the same path in the build directory:
  build products never exist in Git, so the two form the build's view of the tree.
- **Arguments** (`args_for()`): `-nostdinc -isystem <libclang resource include>` (no host header is ever read),
  `-x c` (C) or `-x assembler-with-cpp -D__ASSEMBLY__` (`.S` / `.s`), the target of the configuration
  (`--target=x86_64-linux-gnu` / `i386-linux-gnu` / ...; an ARCH without a `_TARGETS` triple keeps the host
  target when 64-bit and takes `i386-linux-gnu` when 32-bit, so `sizeof(long)` matches `BITS_PER_LONG`; a
  big-endian one (`ArchEnv.is_big_endian()`: CPU_BIG_ENDIAN / CPU_LITTLE_ENDIAN of the configuration, else
  `_BIG_ENDIAN_ARCHES`) takes its own triple when libclang has a backend for it (m68k), else
  `powerpc64-linux-gnu` / `powerpc-linux-gnu`), the layout's flags (`-D__KERNEL__`, `-std=gnu11`, ...;
  `-std=gnu89` when the layout names none), the include directories and the forced `-include` headers (for C all
  of them, for assembly all but `compiler_types.h`, which kbuild adds to `c_flags` only), and the per-object
  `KBUILD_BASENAME` / `KBUILD_MODNAME` / `KBUILD_MODFILE` strings of the file.
- **Options**: `KEEP_GOING` (`CXTranslationUnit_KeepGoing`): a header that stays missing is an error like any other
  instead of a fatal one that silences every later diagnostic.
- **Paths** (`repo_path()`): a file libclang opened, as the repository path it stands for (a symlink of the build
  directory resolves into the tree; a generated header keeps the build path, e.g. `include/generated/autoconf.h`).
- **Diagnostics** (`diagnostics_fact()`): parse ARCH, configuration digest, libclang version, error count and missing
  headers of a translation unit, the `m_blob_facts` family 7 record (`DIAG_FAMILY`).

The process-wide environment of the version being parsed is `active()` (set by `pipeline/compile_envs.py` before
the parse workers fork); without one, `for_tree()` builds and caches one from a directory on disk (tests, tools).
"""
from __future__ import annotations

import atexit
import contextlib
import glob
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import tempfile
import threading
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from core.FileHandler import mark_owner
from core.globalstuff import G
from parser.arch_layout import ArchLayout, arch_values, layout_for, path_arch
from parser.make_eval import SRC_ROOT, UNKNOWN, MakeEvaluator, Tree, tree_path
from parser.generated_headers import config_digest, defconfig_values, generated_headers
from parser.include_graph import IncludeResolver

logger = logging.getLogger(__name__)

DEFAULT_ARCH = "x86_64"
KEEP_GOING = 0x200          # CXTranslationUnit_KeepGoing
DIAG_FAMILY = 7             # m_blob_facts family of the parse diagnostics
DIAG_FORMAT = 1
_MAX_MISSING = 64
_C_ONLY_FORCED = ("include/linux/compiler_types.h",)
HEADER_PRELUDE = "include/linux/types.h"   # retry prelude of a header that is not self-contained (header_prelude())
_INCLUDE_LINE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"]+)[>"]', re.M)
_UNKNOWN_TYPE = "unknown type name"

# --target of an ARCH tree: (64-bit triple, 32-bit triple); None keeps libclang's host target
_TARGETS: dict[str, tuple[str | None, str | None]] = {
    "x86": ("x86_64-linux-gnu", "i386-linux-gnu"), "x86_64": ("x86_64-linux-gnu", "x86_64-linux-gnu"),
    "i386": ("i386-linux-gnu", "i386-linux-gnu"), "um": (None, None),
    "arm64": ("aarch64-linux-gnu", "aarch64-linux-gnu"), "arm": ("arm-linux-gnueabi", "arm-linux-gnueabi"),
    "riscv": ("riscv64-linux-gnu", "riscv32-linux-gnu"), "powerpc": ("powerpc64-linux-gnu", "powerpc-linux-gnu"),
    "ppc64": ("powerpc64-linux-gnu", "powerpc64-linux-gnu"), "ppc": ("powerpc-linux-gnu", "powerpc-linux-gnu"),
    "s390": ("s390x-linux-gnu", "s390x-linux-gnu"), "sparc": ("sparc64-linux-gnu", "sparc-linux-gnu"),
    "sparc64": ("sparc64-linux-gnu", "sparc64-linux-gnu"), "mips": ("mips64-linux-gnu", "mips-linux-gnu"),
    "loongarch": ("loongarch64-linux-gnu", "loongarch64-linux-gnu"), "ia64": (None, None),
}
# --target of a little-endian ARCH without a `_TARGETS` entry (tile, blackfin, sh, ...): the host target for a 64-bit
# configuration, a 32-bit triple for a 32-bit one, so `sizeof(long)` agrees with the configuration's
# `BITS_PER_LONG` (with a 64-bit `long`, `CPU_MASK_ALL`'s `[0 ... BITS_TO_LONGS(NR_CPUS) - 2]` wraps to
# `[0 ... SIZE_MAX]`, a range designator libclang expands element by element without end)
_GENERIC_TARGETS: tuple[str | None, str | None] = (None, "i386-linux-gnu")
# --target of a big-endian ARCH without a `_TARGETS` entry (parisc, h8300, openrisc, sh with CPU_BIG_ENDIAN, ...):
# big-endian triples of the same `long` widths, so `__BYTE_ORDER__` / `__BIG_ENDIAN__` and the byte-order
# branches of the headers follow the configuration (`ArchEnv.is_big_endian()`)
_GENERIC_BE_TARGETS: tuple[str | None, str | None] = ("powerpc64-linux-gnu", "powerpc-linux-gnu")
# Big-endian ARCHes whose own triple libclang may have a backend for: taken when the loaded libclang accepts it
# (`target_supported()`), so the ARCH's inline-assembly constraints ("a" / "d" registers) validate
_NATIVE_BE_TARGETS: dict[str, tuple[str | None, str | None]] = {
    "m68k": ("m68k-linux-gnu", "m68k-linux-gnu"), "m68knommu": ("m68k-linux-gnu", "m68k-linux-gnu"),
}
# Unmapped ARCHes that are big-endian when the configuration names no byte order (their Kconfig has no
# CPU_BIG_ENDIAN / CPU_LITTLE_ENDIAN symbol before 5.0, or none at all); m32r and microblaze default to big-endian
_BIG_ENDIAN_ARCHES = frozenset({
    "m68k", "m68knommu", "parisc", "parisc64", "h8300", "avr32", "frv", "m32r", "openrisc", "microblaze",
})
_BIG_ENDIAN_SYMBOLS = ("CPU_BIG_ENDIAN", "BIG_ENDIAN")
_LITTLE_ENDIAN_SYMBOLS = ("CPU_LITTLE_ENDIAN", "LITTLE_ENDIAN")

_ACTIVE: CompileEnv | None = None
_TARGET_SUPPORT: dict[str, bool] = {}
_TREE_ENVS: dict[str, CompileEnv] = {}
_LOCK = threading.Lock()
_RESOURCE: list[str | None] = []
_VERSION: list[str] = []

_DIR_OWN_VARS = ("ccflags-y", "ccflags-m", "ccflags-", "EXTRA_CFLAGS")
_DIR_SUBDIR_VARS = ("subdir-ccflags-y", "subdir-ccflags-m", "subdir-ccflags-")


def _reset_locks_in_child() -> None:
    global _LOCK
    _LOCK = threading.Lock()
    if _ACTIVE is not None:
        _ACTIVE._lock = threading.Lock()
    for env in list(_TREE_ENVS.values()):
        env._lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_locks_in_child)


def _atexit_cleanup() -> None:
    pid = os.getpid()
    envs: list[CompileEnv] = []
    if _ACTIVE is not None:
        envs.append(_ACTIVE)
    envs.extend(_TREE_ENVS.values())
    for env in envs:
        if getattr(env, "_owner_pid", pid) == pid:
            with contextlib.suppress(Exception):
                env.cleanup()


atexit.register(_atexit_cleanup)


def target_supported(triple: str) -> bool:
    """True when the loaded libclang parses a translation unit for `--target=<triple>` (cached per triple)."""
    known = _TARGET_SUPPORT.get(triple)
    if known is not None:
        return known
    try:
        import clang.cindex as cc
        tu = cc.Index.create().parse("kip_target_probe.c", args=["-nostdinc", f"--target={triple}"],
                                     unsaved_files=[("kip_target_probe.c", "int kip_target_probe;\n")])
        ok = not any(d.severity >= cc.Diagnostic.Error for d in tu.diagnostics)
    except Exception:  # noqa: BLE001 - no libclang, or a triple without a backend (TranslationUnitLoadError)
        ok = False
    _TARGET_SUPPORT[triple] = ok
    return ok


def generic_targets(env: ArchEnv) -> tuple[str | None, str | None]:
    """(64-bit, 32-bit) `--target` pair of an ARCH without a `_TARGETS` entry: `_GENERIC_TARGETS` for a
    little-endian configuration; for a big-endian one the ARCH's `_NATIVE_BE_TARGETS` pair when libclang supports
    it, else `_GENERIC_BE_TARGETS`."""
    if not env.is_big_endian():
        return _GENERIC_TARGETS
    native = _NATIVE_BE_TARGETS.get(env.srcarch) or _NATIVE_BE_TARGETS.get(env.arch)
    if native and all(t is None or target_supported(t) for t in native):
        return native
    return _GENERIC_BE_TARGETS


def clang_resource_include() -> str | None:
    """The `include/` of the loaded libclang's resource directory (stddef.h, stdarg.h), None when not found."""
    if _RESOURCE:
        return _RESOURCE[0]
    with _LOCK:
        if _RESOURCE:
            return _RESOURCE[0]
        found = None
        try:
            import clang.cindex as cc
            lib = cc.conf.lib._name if hasattr(cc.conf.lib, "_name") else ""
        except Exception:  # noqa: BLE001
            lib = ""
        m = re.search(r"libclang-(\d+)", lib or "")
        majors = [m.group(1)] if m else []
        patterns = []
        for major in majors:
            patterns += [f"/usr/lib/llvm-{major}/lib/clang/*/include", f"/usr/lib/clang/{major}*/include",
                         f"/usr/include/clang/{major}*/include", f"/usr/lib64/clang/{major}*/include"]
        patterns += ["/usr/lib/llvm-*/lib/clang/*/include", "/usr/lib/clang/*/include", "/usr/lib64/clang/*/include"]
        for pattern in patterns:
            hits = sorted(h for h in glob.glob(pattern) if os.path.isfile(os.path.join(h, "stddef.h")))
            if hits:
                found = hits[-1]
                break
        _RESOURCE.append(found)
        return found


def libclang_version() -> str:
    """The loaded libclang: library name and resource directory version ("libclang-20.so/20"), "" when unknown."""
    if _VERSION:
        return _VERSION[0]
    resource = clang_resource_include() or ""
    with _LOCK:
        if _VERSION:
            return _VERSION[0]
        try:
            import clang.cindex as cc
            name = os.path.basename(getattr(cc.conf.lib, "_name", "") or "")
        except Exception:  # noqa: BLE001
            name = ""
        _VERSION.append(f"{name}/{os.path.basename(os.path.dirname(resource))}" if name or resource else "")
        return _VERSION[0]


def _build_base() -> str:
    ramdisk = getattr(G, "RAMDISK", None)
    if ramdisk and os.path.isdir(ramdisk) and os.access(ramdisk, os.W_OK):
        base = ramdisk
    elif os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
        base = "/dev/shm"
    else:
        base = tempfile.gettempdir()
    return os.path.join(base, "kip_build")


def _include_name(target: str) -> str:
    """`#include` name of an asm-generic header path (`include/uapi/asm-generic/x.h` -> `asm-generic/x.h`)."""
    for prefix in ("include/uapi/", "include/"):
        if target.startswith(prefix):
            return target[len(prefix):]
    return target


@dataclass(frozen=True)
class TreeFileReader:
    """Picklable file reader reading Latin-1 source files relative to `root`."""
    root: str

    def __call__(self, path: str) -> str | None:
        try:
            with open(os.path.join(self.root, path), encoding="latin-1") as fh:
                return fh.read()
        except OSError:
            return None


@dataclass
class ArchEnv:
    """The build of one ARCH: layout, defconfig, target and build directory."""
    arch: str
    layout: ArchLayout
    config: dict[str, tuple[str, str]] = field(default_factory=dict)
    defconfig: str | None = None
    config_hash: str = ""
    build_dir: str = ""
    target: str | None = None

    @property
    def srcarch(self) -> str:
        return self.layout.srcarch

    def is_64bit(self) -> bool:
        if "64BIT" in self.config:
            return self.config["64BIT"][1] == "y"
        return self.arch.endswith("64") or self.arch in ("x86_64", "arm64", "s390")

    def is_big_endian(self) -> bool:
        """Byte order of the configuration: CPU_BIG_ENDIAN / BIG_ENDIAN when the configuration has one, else
        CPU_LITTLE_ENDIAN / LITTLE_ENDIAN (a big-endian build when it is not set), else `_BIG_ENDIAN_ARCHES`."""
        for name in _BIG_ENDIAN_SYMBOLS:
            if name in self.config:
                return self.config[name][1] == "y"
        for name in _LITTLE_ENDIAN_SYMBOLS:
            if name in self.config:
                return self.config[name][1] != "y"
        return self.srcarch in _BIG_ENDIAN_ARCHES or self.arch in _BIG_ENDIAN_ARCHES


class CompileEnv:
    """libclang arguments of the files of one revision (see the module docstring)."""

    def __init__(self, root: str, files: Iterable[str], read: Callable[[str], str | None],
                 layouts: dict[str, ArchLayout] | None = None, key: str = "", default_arch: str = DEFAULT_ARCH,
                 build_base: str | None = None, configure: bool = True) -> None:
        """`layouts`: the revision's layouts by ARCH; None evaluates them from the tree's Makefiles on first use."""
        self.root = os.path.abspath(root).rstrip("/")
        self._real_root = os.path.realpath(self.root).rstrip("/")
        self.files = files if isinstance(files, set) else set(files)
        self.read = read
        self.key = key or self.root
        self.configure = configure
        self.build_base = os.path.abspath(build_base or _build_base()).rstrip("/")
        self._real_build_base = os.path.realpath(self.build_base).rstrip("/")
        self._owner_pid = os.getpid()
        self._lock = threading.Lock()
        self._envs: dict[str, ArchEnv] = {}
        self._tree: Tree | None = None
        self._parsed: dict = {}
        self._dir_own_includes: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._dir_includes_cache: dict[str, tuple[str, ...]] = {}
        self._resolvers: dict[str, IncludeResolver] = {}
        self._prelude_closure: dict[str, frozenset[str]] = {}
        if layouts is not None:
            self.arch_values = sorted(layouts)
            self._layouts: dict[str, ArchLayout | None] = dict(layouts)
        else:
            top = read("Makefile") if "Makefile" in self.files else None
            self.arch_values = arch_values(self._tree_view(), top) if top is not None else []
            self._layouts = {}
        self.default_arch = self._pick_default(default_arch)

    def _tree_view(self) -> Tree:
        if self._tree is None:
            self._tree = Tree(self.files)
        return self._tree

    def layout(self, arch: str | None) -> ArchLayout | None:
        """Layout of an ARCH value (evaluated once), None when the revision does not build it."""
        if not arch or arch not in self.arch_values:
            return None
        if arch in self._layouts:
            return self._layouts[arch]
        with self._lock:
            if arch not in self._layouts:
                try:
                    self._layouts[arch] = layout_for(arch, self._tree_view(), self.read, parsed=self._parsed)
                except Exception as exc:  # noqa: BLE001 - one ARCH's Makefiles must not stop the parse
                    logger.warning("ARCH=%s: Makefile evaluation failed: %s", arch, exc)
                    self._layouts[arch] = None
            return self._layouts[arch]

    # ------------------------------------------------------------------ per-directory -I flags
    @staticmethod
    def _extract_inc_dirs(text: str, directory: str, tree: Tree) -> list[str]:
        if not text:
            return []
        tokens = [w for w in text.split() if UNKNOWN not in w]
        out: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-I", "-iquote", "-isystem") and i + 1 < len(tokens):
                raw_val, i = tokens[i + 1], i + 2
            elif tok.startswith("-I") and len(tok) > 2:
                raw_val, i = tok[2:], i + 1
            elif tok.startswith("-iquote") and len(tok) > 7:
                raw_val, i = tok[7:], i + 1
            else:
                i += 1
                continue
            val = raw_val.strip().strip("\"'")
            if not val or "$" in val or UNKNOWN in val:
                continue
            while val.startswith(f"{SRC_ROOT}/{SRC_ROOT}"):
                val = val[len(SRC_ROOT) + 1:]
            anchored = val.startswith((SRC_ROOT, "/"))
            candidates: list[str | None] = []
            if anchored:
                candidates.append(tree_path(val))
            else:
                candidates.append(tree_path(val))
                if directory:
                    candidates.append(tree_path(val, directory))
            resolved: str | None = None
            for cand in candidates:
                if cand is not None and (cand == "" or tree.is_dir(cand) or "generated" in cand.split("/")):
                    resolved = cand
                    break
            if resolved is None and candidates and candidates[0] is not None and not directory:
                resolved = candidates[0]
            if resolved is not None and resolved not in out:
                out.append(resolved)
        return out

    def _eval_dir_includes(self, directory: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(own -I dirs from ccflags-y/EXTRA_CFLAGS, inheritable -I dirs from subdir-ccflags-y) of `directory`."""
        if not directory:
            return (), ()
        kbuild_path = f"{directory}/Kbuild"
        makefile_path = f"{directory}/Makefile"
        has_kbuild = kbuild_path in self.files
        has_makefile = makefile_path in self.files
        if not has_kbuild and not has_makefile:
            return (), ()
        tree = self._tree_view()
        cmdline = {
            "srctree": SRC_ROOT,
            "objtree": SRC_ROOT,
            "TOPDIR": SRC_ROOT,
            "CURDIR": SRC_ROOT,
            "src": directory,
            "obj": directory,
        }
        ev = MakeEvaluator(tree, self.read, cmdline, {}, parsed={})
        try:
            # In kbuild (scripts/Makefile.build), Kbuild is included first if present, else Makefile
            primary = kbuild_path if has_kbuild else makefile_path
            ev.include(primary)
            if has_kbuild and has_makefile and makefile_path not in ev. words("MAKEFILE_LIST"):
                ev.include(makefile_path)
        except (RecursionError, Exception) as exc:  # noqa: BLE001
            logger.debug("Directory Makefile evaluation failed for %s: %s", directory, exc)
            return (), ()
        own_text = " ".join(ev.value(v) for v in _DIR_OWN_VARS if ev.defined(v))
        sub_text = " ".join(ev.value(v) for v in _DIR_SUBDIR_VARS if ev.defined(v))
        own_dirs = tuple(self._extract_inc_dirs(own_text, directory, tree))
        sub_dirs = tuple(self._extract_inc_dirs(sub_text, directory, tree))
        return own_dirs, sub_dirs

    def _dir_includes(self, directory: str) -> tuple[str, ...]:
        """All local `-I` directories for `directory` (inherited `subdir-ccflags-y` from ancestors + own flags)."""
        directory = (directory or "").strip("/")
        if not directory:
            return ()
        cached = self._dir_includes_cache.get(directory)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._dir_includes_cache.get(directory)
            if cached is not None:
                return cached
            parts = directory.split("/")
            ancestors = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
            merged: list[str] = []
            for idx, anc in enumerate(ancestors):
                pair = self._dir_own_includes.get(anc)
                if pair is None:
                    pair = self._eval_dir_includes(anc)
                    self._dir_own_includes[anc] = pair
                own_dirs, sub_dirs = pair
                for d in sub_dirs:
                    if d not in merged:
                        merged.append(d)
                if idx == len(ancestors) - 1:
                    for d in own_dirs:
                        if d not in merged:
                            merged.append(d)
            res = tuple(merged)
            self._dir_includes_cache[directory] = res
            return res

    # ------------------------------------------------------------------ construction
    @classmethod
    def for_tree(cls, root: str, key: str = "", configure: bool = True) -> CompileEnv:
        """Environment of an extracted tree on disk (cached per directory and key)."""
        root = os.path.abspath(root).rstrip("/")
        eff_key = key
        if not eff_key:
            with contextlib.suppress(OSError):
                st = os.stat(os.path.join(root, "Makefile"))
                eff_key = f"{root}:{st.st_mtime_ns}:{st.st_size}"
        cache_key = f"{root}\0{eff_key}\0{configure}"
        with _LOCK:
            env = _TREE_ENVS.get(cache_key)
            if env is not None:
                return env
        files: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            rel = os.path.relpath(dirpath, root)
            for name in filenames:
                files.add(name if rel == "." else f"{rel}/{name}")

        env = cls(root, files, TreeFileReader(root), None, key=eff_key or root, configure=configure)
        with _LOCK:
            existing = _TREE_ENVS.get(cache_key)
            if existing is not None:
                return existing
            _TREE_ENVS[cache_key] = env
        return env

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state.pop("_lock", None)
        state["_resolvers"] = {}
        if not isinstance(state.get("read"), TreeFileReader):
            state["read"] = TreeFileReader(self.root)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()
        self.__dict__.setdefault("_resolvers", {})
        self.__dict__.setdefault("_prelude_closure", {})

    def _pick_default(self, preferred: str) -> str | None:
        for name in (preferred, "x86_64", "i386", "x86", *self.arch_values):
            if self.layout(name) is not None:
                return name
        return None

    # ------------------------------------------------------------------ ARCH of a file
    def arch_for(self, path: str) -> str | None:
        """ARCH value a file is parsed for (None when the revision has no layout)."""
        default = self.default_arch
        tree = path_arch(path)
        if default is None or tree is None:
            return default
        base = self.layout(default)
        if base is not None and (base.srcarch == tree or tree in base.arch_trees()):
            return default
        if self.layout(tree) is not None:
            return tree
        return default

    def env(self, arch: str | None) -> ArchEnv | None:
        """The (materialized) build of an ARCH, None without a layout."""
        if self.layout(arch) is None:
            return None
        env = self._envs.get(arch)
        if env is not None:
            return env
        with self._lock:
            env = self._envs.get(arch)
            if env is None:
                env = self._make_env(arch)
                self._envs[arch] = env
        return env

    def prepare(self, arches: Iterable[str]) -> None:
        """Materialize the builds of `arches` now (before forking parse workers)."""
        for arch in arches:
            self.env(arch)

    def _make_env(self, arch: str) -> ArchEnv:
        layout = self.layout(arch)
        assert layout is not None
        config, defconfig = (defconfig_values(self.files, self.read, arch, layout.srcarch)
                             if self.configure else ({}, None))
        env = ArchEnv(arch, layout, config, defconfig, config_digest(config))
        bits64 = env.is_64bit()
        pair = _TARGETS.get(layout.srcarch) or _TARGETS.get(arch)
        if pair is None:
            pair = generic_targets(env)
        env.target = pair[0] if bits64 else pair[1]
        digest = hashlib.sha1(f"{self.key}\0{arch}\0{env.config_hash}".encode("utf-8", "surrogatepass")).hexdigest()[:16]
        env.build_dir = os.path.join(self.build_base, f"{digest}-{arch}")
        self._materialize(env)
        return env

    def _materialize(self, env: ArchEnv) -> None:
        build = env.build_dir
        stamp = os.path.join(build, ".kip_complete")
        if os.path.exists(stamp):
            return
        tmp = f"{build}.tmp.{os.getpid()}.{threading.get_ident()}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.makedirs(tmp, exist_ok=True)
            mark_owner(self.build_base)
            mark_owner(tmp)

            def put(path: str, text: str) -> None:
                dest = os.path.join(tmp, path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="latin-1", errors="replace") as fh:
                    fh.write(text)

            for link, target in sorted(env.layout.links.items()):
                dest = os.path.join(tmp, link.rstrip("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if not os.path.lexists(dest):
                    os.symlink(os.path.join(self.root, target.rstrip("/")), dest)
            for wrapper, target in sorted(env.layout.wrappers.items()):
                if wrapper not in self.files:
                    put(wrapper, f"#include <{_include_name(target)}>\n")
            for path, text in generated_headers(self.files, self.read, env.arch, env.srcarch, env.config).items():
                if not os.path.lexists(os.path.join(tmp, path)):
                    put(path, text)
            with open(os.path.join(tmp, ".kip_complete"), "w") as fh:
                fh.write(json.dumps({"arch": env.arch, "defconfig": env.defconfig, "config": env.config_hash, "key": self.key}))
            try:
                os.rename(tmp, build)
            except OSError:  # another process materialized it first
                shutil.rmtree(tmp, ignore_errors=True)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def cleanup(self) -> None:
        """Remove the build directories this environment created (including any materialized by forked workers)."""
        with self._lock:
            envs = list(self._envs.values())
            self._envs.clear()
        for env in envs:
            shutil.rmtree(env.build_dir, ignore_errors=True)
        if os.path.isdir(self.build_base):
            for entry in glob.glob(os.path.join(self.build_base, "*")):
                stamp = os.path.join(entry, ".kip_complete")
                if not os.path.isfile(stamp):
                    continue
                try:
                    with open(stamp, encoding="utf-8") as fh:
                        meta = json.load(fh)
                    if meta.get("key") == self.key:
                        shutil.rmtree(entry, ignore_errors=True)
                except (OSError, ValueError, TypeError):
                    pass

    # ------------------------------------------------------------------ arguments
    def args_for(self, path: str, asm: bool = False, arch: str | None = None) -> tuple[list[str], int, ArchEnv | None]:
        """(libclang arguments, extra parse options, ARCH build) of `path` (repository-relative). A `.S` / `.s`
        file is always an assembler-with-cpp compilation, whichever parser reads it."""
        asm = asm or path.endswith((".S", ".s"))
        env = self.env(arch or self.arch_for(path))
        args: list[str] = ["-nostdinc"]
        resource = clang_resource_include()
        if resource:
            args += ["-isystem", resource]
        if asm:
            args += ["-x", "assembler-with-cpp", "-D__ASSEMBLY__"]
        else:
            args += ["-x", "c"]
        if env is None:
            # No layout (a tree without Makefiles): the source directory and include/
            inc_dir = path.rpartition("/")[0]
            args += ["-D__KERNEL__", f"-I{self.root}/{inc_dir}" if inc_dir else f"-I{self.root}", f"-I{self.root}/include"]
            for d in self._dir_includes(inc_dir):
                if d not in (inc_dir, "include"):
                    args.append(f"-I{self.root}/{d}" if d else f"-I{self.root}")
            if not asm:
                args.append("-std=gnu89")
            return args, KEEP_GOING, None
        if env.target:
            args.append(f"--target={env.target}")
        flags = list(env.layout.flags)
        if "-D__KERNEL__" not in flags:
            flags.insert(0, "-D__KERNEL__")
        for flag in flags:
            if asm and not flag.startswith(("-D", "-U")):
                continue
            args.append(flag)
        if not asm and not any(f.startswith("-std=") for f in flags):
            args.append("-std=gnu89")
        # Per-object defines of scripts/Makefile.lib (the module name is the object's basename)
        base = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", "_")
        args += [f'-DKBUILD_BASENAME="{base}"', f'-DKBUILD_MODNAME="{base}"',
                 f'-DKBUILD_MODFILE="{path.rsplit(".", 1)[0]}"']
        inc_dir = path.rpartition("/")[0]
        args += ["-iquote", f"{self.root}/{inc_dir}" if inc_dir else self.root]
        seen_inc: set[str] = set()
        for d in env.layout.includes:
            d = d.rstrip("/")
            seen_inc.add(d)
            args.append(f"-I{self.root}/{d}" if d else f"-I{self.root}")
            args.append(f"-I{env.build_dir}/{d}" if d else f"-I{env.build_dir}")
        for d in self._dir_includes(inc_dir):
            d = d.rstrip("/")
            if d in seen_inc:
                continue
            seen_inc.add(d)
            args.append(f"-I{self.root}/{d}" if d else f"-I{self.root}")
            args.append(f"-I{env.build_dir}/{d}" if d else f"-I{env.build_dir}")
        for forced in env.layout.forced:
            if asm and forced in _C_ONLY_FORCED:
                continue
            src = os.path.join(self.root, forced)
            if forced not in self.files:
                src = os.path.join(env.build_dir, forced)
                if not os.path.exists(src):
                    continue
            args += ["-include", src]
        return args, KEEP_GOING, env

    def header_prelude(self, path: str, arch_env: ArchEnv | None) -> list[str]:
        """`-include` arguments of the header prelude (`HEADER_PRELUDE`) for a C header `path`, [] when none applies.

        A header the kernel only reaches after `<linux/types.h>` (`u32` in `include/linux/lockd/bind.h`) is not
        self-contained; parsed alone, its primitive kernel types are unknown and every declaration using one is a
        syntax error. The prelude is the retry of such a parse (`parser/c_ast/c_ast.py`): never for a non-header,
        without an ARCH build, or for a file the prelude itself reads (its static `#include` closure, which a
        forced copy would hide behind its include guard)."""
        if arch_env is None or not path.endswith(".h") or HEADER_PRELUDE not in self.files:
            return []
        if path in self._prelude_files(arch_env):
            return []
        return ["-include", os.path.join(self.root, HEADER_PRELUDE)]

    def _prelude_files(self, arch_env: ArchEnv) -> frozenset[str]:
        """Tree files `HEADER_PRELUDE` reaches through `#include` lines (every branch of `#if`: a superset)."""
        closure = self._prelude_closure.get(arch_env.arch)
        if closure is not None:
            return closure
        seen: set[str] = set()
        todo = [HEADER_PRELUDE]
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            for quote, name in _INCLUDE_LINE.findall(self.read(current) or ""):
                hit = self.resolve_include(name, current, quoted=quote == '"', arch_env=arch_env)
                if hit and hit not in seen:
                    todo.append(hit)
        closure = frozenset(seen)
        self._prelude_closure[arch_env.arch] = closure
        return closure

    # ------------------------------------------------------------------ paths
    def resolve_include(self, name: str, including_file: str, quoted: bool = False,
                        arch_env: ArchEnv | None = None) -> str | None:
        """Tree file an `#include` of `name` in `including_file` reads, None when the tree has none.

        Searches what `args_for()` gives libclang: the including file's directory (quoted form), the layout's
        include directories (through its links and `asm-generic` wrappers, `parser/include_graph.py`
        `IncludeResolver`), then the directory's own `-I` directories. Without a layout: the including file's
        directory, `include/` and the directory's `-I` directories.
        """
        name = (name or "").strip()
        if not name or name.startswith("/"):
            return None
        inc_dir = (including_file or "").rpartition("/")[0]
        if arch_env is not None:
            resolver = self._resolvers.get(arch_env.arch)
            if resolver is None:
                resolver = IncludeResolver(arch_env.layout, self.files.__contains__)
                self._resolvers[arch_env.arch] = resolver
            hit = resolver.resolve(name, including_file, quoted=quoted)
            if hit:
                return hit
            check = resolver.materialize
            dirs: tuple[str, ...] = self._dir_includes(inc_dir)
        else:
            def check(path: str) -> str | None:
                return path if path in self.files else None
            dirs = (inc_dir, "include", *self._dir_includes(inc_dir))
        for d in dirs:
            d = d.rstrip("/")
            candidate = posixpath.normpath(posixpath.join(d, name)) if d else posixpath.normpath(name)
            if candidate == ".." or candidate.startswith("../"):
                continue
            hit = check(candidate)
            if hit:
                return hit
        return None

    def repo_path(self, file_path: str) -> str | None:
        """Repository path of a file libclang opened, None for a file outside the tree and the builds."""
        if not file_path:
            return None
        real = os.path.realpath(file_path)
        for rroot in (self._real_root, self.root):
            if rroot and real.startswith(rroot + "/"):
                return real[len(rroot) + 1:]
        absolute = os.path.abspath(file_path)
        if absolute.startswith(self.root + "/"):
            return absolute[len(self.root) + 1:]
        for bbase, candidate in ((self.build_base, absolute), (self._real_build_base, real)):
            base = bbase.rstrip("/") + "/"
            if candidate.startswith(base):
                rest = candidate[len(base):]
                return rest.split("/", 1)[1] if "/" in rest else None
        return None


# ---------------------------------------------------------------------------------------------- process state
def set_active(env: CompileEnv | None) -> None:
    global _ACTIVE
    _ACTIVE = env


def active() -> CompileEnv | None:
    return _ACTIVE


def env_for(root: str) -> CompileEnv:
    """The active environment when it describes `root`, else the cached one of the directory."""
    env = _ACTIVE
    if env is not None and env.root == os.path.abspath(root).rstrip("/"):
        return env
    return CompileEnv.for_tree(root)


# ---------------------------------------------------------------------------------------------- diagnostics
_MISSING_RE = re.compile(r"'([^']+)' file not found")


def diagnostics_fact(tu: Any, env: ArchEnv | None, asm: bool = False) -> dict[str, Any]:
    """Family 7 record of a translation unit: {arch, defconfig, config, libclang, errors, missing} (the C parser
    adds `prelude`, the `HEADER_PRELUDE` path, to the record of a header parsed after it).

    For assembly (`asm`), libclang reads the preprocessed text as C: only preprocessor errors count.
    """
    errors = 0
    missing: list[str] = []
    try:
        for d in tu.diagnostics:
            if d.severity >= 3:
                m = _MISSING_RE.search(d.spelling)
                if asm and not m and "#error" not in d.spelling and d.category_name != "Lexical or Preprocessor Issue":
                    continue
                errors += 1
                if m and m.group(1) not in missing and len(missing) < _MAX_MISSING:
                    missing.append(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return {
        "arch": env.arch if env else None,
        "defconfig": env.defconfig if env else None,
        "config": env.config_hash if env else "",
        "libclang": libclang_version(),
        "errors": errors,
        "missing": missing,
    }


def has_unknown_type(tu: Any, main_file: str) -> bool:
    """True when libclang reports an unknown type name in `main_file` itself (a header that is not self-contained)."""
    try:
        for d in tu.diagnostics:
            if d.severity >= 3 and d.spelling.startswith(_UNKNOWN_TYPE):
                f = d.location.file
                if f is not None and f.name == main_file:
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def encode_fact(record: dict[str, Any]) -> bytes:
    return zlib.compress(json.dumps(record, separators=(",", ":"), sort_keys=True).encode(), 3)


def decode_fact(data: bytes | bytearray | memoryview | None) -> dict[str, Any] | None:
    if not data:
        return None
    try:
        return json.loads(zlib.decompress(bytes(data)))
    except (zlib.error, ValueError, TypeError):
        return None
