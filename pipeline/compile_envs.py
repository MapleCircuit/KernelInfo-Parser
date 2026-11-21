"""Step 4.6: the compilation environment of the version's parse (`parser/compile_env.py`).

Before the parse workers fork, the version's `CompileEnv` is built over the checked-out workspace and the version's
tree (`gp.file_blob_hashes`): the definition ARCH and the ARCH of every changed file get their layout, defconfig and
build directory (links, `asm-generic` wrappers, generated headers). The forked workers inherit it
(`compile_env.active()`), so every C and assembly file is parsed with the arguments `make ARCH=<arch>` would use.
`release_compile_env()` removes the build directories once the version's files are parsed.
"""
from __future__ import annotations

import os
import time

from parser import compile_env
from pipeline.state import MF, gp, logger


def _reader(root: str) -> compile_env.TreeFileReader:
    return compile_env.TreeFileReader(root)


def prepare_compile_env(version: str, change_lines: list[str] | None) -> compile_env.CompileEnv | None:
    """Build and activate the version's environment; None (and none active) without a workspace or Makefile."""
    release_compile_env()
    root = MF.version_dict.get(version) if hasattr(MF, "version_dict") else None
    files = getattr(gp, "file_blob_hashes", None) or {}
    if not root or not os.path.isdir(root) or "Makefile" not in files:
        return None
    started = time.perf_counter()
    env = compile_env.CompileEnv(root, set(files), _reader(root), None, key=f"{root}:{version}")
    compile_env.set_active(env)
    arches = {env.default_arch}
    for line in change_lines or ():
        parts = line.split("\t") if line else []
        if not parts or parts[0].startswith("D"):
            continue
        path = parts[-1]
        if path.endswith((".c", ".h", ".S", ".s")):
            arches.add(env.arch_for(path))
    arches.discard(None)
    try:
        env.prepare(sorted(arches))
    except BaseException:
        release_compile_env()
        raise
    extra_arches = len(arches - {env.default_arch})
    logger.info(f"[Compile Env] '{version}': ARCH {env.default_arch} (+{extra_arches} file ARCHes) prepared "
                f"in {time.perf_counter() - started:.1f}s.")
    return env


def release_compile_env() -> None:
    """Remove the active environment's build directories and deactivate it."""
    env = compile_env.active()
    if env is not None:
        env.cleanup()
    compile_env.set_active(None)
