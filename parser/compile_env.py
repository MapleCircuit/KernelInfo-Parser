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
  (`--target=x86_64-linux-gnu` / `i386-linux-gnu` / ...), the layout's flags (`-D__KERNEL__`, `-std=gnu11`, ...;
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

import glob
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from parser.arch_layout import ArchLayout, arch_values, layout_for, path_arch
from parser.make_eval import Tree
from parser.generated_headers import config_digest, defconfig_values, generated_headers

logger = logging.getLogger(__name__)

DEFAULT_ARCH = "x86_64"
KEEP_GOING = 0x200          # CXTranslationUnit_KeepGoing
DIAG_FAMILY = 7             # m_blob_facts family of the parse diagnostics
DIAG_FORMAT = 1
_MAX_MISSING = 64
_C_ONLY_FORCED = ("include/linux/compiler_types.h",)

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

_ACTIVE: CompileEnv | None = None
_TREE_ENVS: dict[str, CompileEnv] = {}
_LOCK = threading.Lock()
_RESOURCE: list[str | None] = []
_VERSION: list[str] = []


def clang_resource_include() -> str | None:
    """The `include/` of the loaded libclang's resource directory (stddef.h, stdarg.h), None when not found."""
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
    try:
        import clang.cindex as cc
        name = os.path.basename(getattr(cc.conf.lib, "_name", "") or "")
    except Exception:  # noqa: BLE001
        name = ""
    resource = clang_resource_include() or ""
    _VERSION.append(f"{name}/{os.path.basename(os.path.dirname(resource))}" if name or resource else "")
    return _VERSION[0]


def _build_base() -> str:
    base = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else tempfile.gettempdir()
    return os.path.join(base, "kip_build")


def _include_name(target: str) -> str:
    """`#include` name of an asm-generic header path (`include/uapi/asm-generic/x.h` -> `asm-generic/x.h`)."""
    for prefix in ("include/uapi/", "include/"):
        if target.startswith(prefix):
            return target[len(prefix):]
    return target


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


class CompileEnv:
    """libclang arguments of the files of one revision (see the module docstring)."""

    def __init__(self, root: str, files: Iterable[str], read: Callable[[str], str | None],
                 layouts: dict[str, ArchLayout] | None = None, key: str = "", default_arch: str = DEFAULT_ARCH,
                 build_base: str | None = None, configure: bool = True) -> None:
        """`layouts`: the revision's layouts by ARCH; None evaluates them from the tree's Makefiles on first use."""
        self.root = os.path.abspath(root).rstrip("/")
        self.files = files if isinstance(files, set) else set(files)
        self.read = read
        self.key = key or self.root
        self.configure = configure
        self.build_base = build_base or _build_base()
        self._lock = threading.Lock()
        self._envs: dict[str, ArchEnv] = {}
        self._tree: Tree | None = None
        self._parsed: dict = {}
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
        if arch not in self._layouts:
            try:
                self._layouts[arch] = layout_for(arch, self._tree_view(), self.read, parsed=self._parsed)
            except Exception as exc:  # noqa: BLE001 - one ARCH's Makefiles must not stop the parse
                logger.warning("ARCH=%s: Makefile evaluation failed: %s", arch, exc)
                self._layouts[arch] = None
        return self._layouts[arch]

    # ------------------------------------------------------------------ construction
    @classmethod
    def for_tree(cls, root: str, key: str = "", configure: bool = True) -> CompileEnv:
        """Environment of an extracted tree on disk (cached per directory and key)."""
        root = os.path.abspath(root).rstrip("/")
        cache_key = f"{root}\0{key}\0{configure}"
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

        def read(path: str) -> str | None:
            try:
                with open(os.path.join(root, path), encoding="latin-1") as fh:
                    return fh.read()
            except OSError:
                return None

        env = cls(root, files, read, None, key=key or root, configure=configure)
        with _LOCK:
            _TREE_ENVS[cache_key] = env
        return env

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
        env.target = (pair[0] if bits64 else pair[1]) if pair else None
        digest = hashlib.sha1(f"{self.key}\0{arch}\0{env.config_hash}".encode("utf-8", "surrogatepass")).hexdigest()[:16]
        env.build_dir = os.path.join(self.build_base, f"{digest}-{arch}")
        self._materialize(env)
        return env

    def _materialize(self, env: ArchEnv) -> None:
        build = env.build_dir
        stamp = os.path.join(build, ".kip_complete")
        if os.path.exists(stamp):
            return
        tmp = f"{build}.tmp{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)

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
            fh.write(json.dumps({"arch": env.arch, "defconfig": env.defconfig, "config": env.config_hash}))
        try:
            os.rename(tmp, build)
        except OSError:  # another process materialized it first
            shutil.rmtree(tmp, ignore_errors=True)

    def cleanup(self) -> None:
        """Remove the build directories this environment created."""
        for env in self._envs.values():
            shutil.rmtree(env.build_dir, ignore_errors=True)
        self._envs.clear()

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
        for d in env.layout.includes:
            d = d.rstrip("/")
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

    # ------------------------------------------------------------------ paths
    def repo_path(self, file_path: str) -> str | None:
        """Repository path of a file libclang opened, None for a file outside the tree and the builds."""
        if not file_path:
            return None
        real = os.path.realpath(file_path)
        if real.startswith(self.root + "/"):
            return real[len(self.root) + 1:]
        absolute = os.path.abspath(file_path)
        if absolute.startswith(self.root + "/"):
            return absolute[len(self.root) + 1:]
        base = self.build_base.rstrip("/") + "/"
        if absolute.startswith(base):
            rest = absolute[len(base):]
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
    """Family 7 record of a translation unit: {arch, defconfig, config, libclang, errors, missing}.

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


def encode_fact(record: dict[str, Any]) -> bytes:
    return zlib.compress(json.dumps(record, separators=(",", ":"), sort_keys=True).encode(), 3)


def decode_fact(data: bytes | bytearray | memoryview | None) -> dict[str, Any] | None:
    if not data:
        return None
    try:
        return json.loads(zlib.decompress(bytes(data)))
    except (zlib.error, ValueError, TypeError):
        return None
